"""Project-defined metrics: read on their interval or on demand, validated, thresholded, kept as history."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, Literal, override

import httpx
from pydantic import BaseModel, Field, ValidationError

from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, ProjectRef, Service, action
from forge_dashboard.core.ui import ui
from forge_dashboard.domain.commands import render_command
from forge_dashboard.domain.summary import MetricsFacts, PinnedMetric
from forge_dashboard.manifest import MetricReading
from forge_dashboard.manifest.models import (
    CommandSource,
    FileSource,
    HttpSource,
    Metric,
    MetricDisplay,
    MetricThresholds,
)
from forge_dashboard.process import tree
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

SPARKLINE_POINTS = 30
ServiceUrlResolver = Callable[[str, str], str | None]
STATUS_UI = ui(format="badge", tones={"ok": "good", "warn": "warn", "fail": "bad"})
MetricStatus = Literal["ok", "warn", "fail"]


def apply_thresholds(reading: MetricReading, thresholds: MetricThresholds | None) -> MetricReading:
    """Fill in `status` from thresholds when the reading did not set one; fail bounds win over warn bounds."""
    if reading.status is not None or thresholds is None:
        return reading
    if isinstance(reading.value, bool) or not isinstance(reading.value, int | float):
        return reading
    value = float(reading.value)
    if (thresholds.fail_below is not None and value < thresholds.fail_below) or (
        thresholds.fail_above is not None and value > thresholds.fail_above
    ):
        return reading.model_copy(update={"status": "fail"})
    if (thresholds.warn_below is not None and value < thresholds.warn_below) or (
        thresholds.warn_above is not None and value > thresholds.warn_above
    ):
        return reading.model_copy(update={"status": "warn"})
    status = "ok" if any(bound is not None for bound in thresholds.model_dump().values()) else None
    return reading.model_copy(update={"status": status})


class MetricError(Exception):
    """A reading could not be taken; the message is shown with the metric."""


INTERVALS_NAMESPACE = "metric-intervals"


class LatestReading(BaseModel):
    metric_id: str
    label: str
    display: MetricDisplay
    unit: str | None = None
    pinned: bool
    value: bool | int | float | str | None = None
    status: MetricStatus | None = Field(None, json_schema_extra=STATUS_UI)
    detail: str | None = None
    error: str | None = None
    read_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    interval_s: int = Field(json_schema_extra=ui(format="duration"))


class ProjectMetrics(BaseModel):
    project_id: str
    metrics: list[LatestReading] = Field(
        default_factory=list, json_schema_extra=ui(columns=["label", "value", "status"])
    )
    interval_overrides: dict[str, int] = Field(
        default_factory=dict,
        json_schema_extra=ui(help="Dashboard overrides of the manifest's intervals, by metric id."),
    )


class MetricsConfig(BaseModel):
    concurrency: int = Field(4, ge=1, le=16, json_schema_extra=ui(advanced=True, help="Readings taken at once."))


class MetricsState(BaseModel):
    projects: list[ProjectMetrics] = Field(default_factory=list)


class MetricInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    metric_id: str


class RefreshInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    metric_id: str | None = Field(None, json_schema_extra=ui(help="One metric; empty refreshes all."))


class HistoryInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    metric_id: str
    limit: int = Field(200, ge=1, le=5000)


class IntervalOverride(BaseModel):
    metric_id: str
    interval_s: int = Field(ge=30, json_schema_extra=ui(widget="duration", help="How often the dashboard reads it."))


class IntervalsInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    overrides: list[IntervalOverride] = Field(
        default_factory=list,
        json_schema_extra=ui(help="Per-metric reading intervals; a metric not listed uses the manifest's interval_s."),
    )


class MetricIntervals(BaseModel):
    project_id: str
    overrides: list[IntervalOverride]


class MetricPoint(BaseModel):
    read_at: float = Field(json_schema_extra=ui(format="relative-time"))
    value: bool | int | float | str | None
    status: MetricStatus | None = Field(None, json_schema_extra=STATUS_UI)
    error: str | None = None


class MetricHistory(BaseModel):
    metric_id: str
    points: list[MetricPoint] = Field(json_schema_extra=ui(columns=["read_at", "value", "status", "error"]))


class MetricThresholdCrossed(Event):
    type: ClassVar[str] = "metrics.status"
    persist: ClassVar[bool] = True
    metric_id: str
    label: str
    status: str
    value: bool | int | float | str | None

    def title(self) -> str:
        return f"Metric {self.label} is {self.status} ({self.value})"

    def severity(self) -> Severity:
        return "error" if self.status == "fail" else "warning" if self.status == "warn" else "info"


class MetricsService(Service[MetricsConfig, MetricsState], ProjectListener):
    id = "metrics"
    title = "Metrics"
    description = "Reads project-defined metrics on their interval, applies thresholds, and keeps history."
    Config = MetricsConfig
    State = MetricsState
    events = (MetricThresholdCrossed,)

    def __init__(self, runtime: Runtime, projects: ProjectsService, service_url: ServiceUrlResolver) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._service_url = service_url
        self._latest: dict[str, dict[str, LatestReading]] = {}
        self._intervals: dict[str, dict[str, int]] = {}
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._limit = asyncio.Semaphore(self.config.concurrency)
        projects.listen(self)

    @override
    async def stop(self) -> None:
        for task in self._loops.values():
            task.cancel()
        for task in self._loops.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @override
    async def on_config_changed(self, previous: MetricsConfig) -> None:
        self._limit = asyncio.Semaphore(self.config.concurrency)

    # --- registry hooks ---

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        self._stop_loops(record.id)
        stored = await self.runtime.db.kv_get(INTERVALS_NAMESPACE, record.id)
        self._intervals[record.id] = (
            {item.metric_id: item.interval_s for item in MetricIntervals.model_validate(stored).overrides}
            if stored is not None
            else {}
        )
        metrics = self._metrics(record)
        latest: dict[str, LatestReading] = {}
        for metric in metrics:
            latest[metric.id] = await self._load_latest(record.id, metric)
        self._latest[record.id] = latest
        await self._publish(record.id)
        for metric in metrics:
            self._loops[f"{record.id}:{metric.id}"] = asyncio.create_task(
                self._loop(record, metric), name=f"metric:{record.id}:{metric.id}"
            )

    @override
    async def project_removed(self, project_id: str) -> None:
        self._stop_loops(project_id)
        self._latest.pop(project_id, None)
        self._intervals.pop(project_id, None)
        self._publish_state()

    def _metrics(self, record: ProjectRecord) -> list[Metric]:
        """The manifest's metrics with the dashboard's interval overrides applied."""
        overrides = self._intervals.get(record.id, {})
        metrics = record.manifest.metrics if record.manifest is not None else []
        return [
            metric.model_copy(update={"interval_s": overrides[metric.id]}) if metric.id in overrides else metric
            for metric in metrics
        ]

    def _stop_loops(self, project_id: str) -> None:
        for key in [key for key in self._loops if key.startswith(f"{project_id}:")]:
            self._loops.pop(key).cancel()

    async def _loop(self, record: ProjectRecord, metric: Metric) -> None:
        latest = self._latest[record.id].get(metric.id)
        if latest is not None and latest.read_at is not None:
            await asyncio.sleep(max(0.0, latest.read_at + metric.interval_s - time.time()))
        while True:
            await self.read(record, metric)
            await asyncio.sleep(metric.interval_s)

    # --- reading ---

    async def read(self, record: ProjectRecord, metric: Metric) -> LatestReading:
        """Take one reading, store it, and publish it."""
        async with self._limit:
            try:
                reading = apply_thresholds(await self._take(record, metric), metric.thresholds)
                error = None
            except MetricError as failure:
                reading, error = None, str(failure)
        now = time.time()
        await self.runtime.db.execute(
            "INSERT INTO metric_readings (project_id, metric_id, ts, value, status, detail, error) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                record.id,
                metric.id,
                now,
                json.dumps(reading.value) if reading is not None else None,
                reading.status if reading is not None else None,
                reading.detail if reading is not None else None,
                error,
            ),
        )
        previous = self._latest.get(record.id, {}).get(metric.id)
        latest = self._latest_from(metric, reading, error, now)
        self._latest.setdefault(record.id, {})[metric.id] = latest
        if (
            reading is not None
            and reading.status in ("warn", "fail")
            and (previous is None or previous.status != reading.status)
        ):
            self.publish(
                MetricThresholdCrossed(
                    project_id=record.id,
                    metric_id=metric.id,
                    label=metric.label,
                    status=reading.status,
                    value=reading.value,
                )
            )
        await self._publish(record.id)
        return latest

    async def _take(self, record: ProjectRecord, metric: Metric) -> MetricReading:
        source = metric.source
        if isinstance(source, CommandSource):
            command = render_command(
                argv=source.argv,
                cwd=source.cwd,
                env={},
                project_root=record.path,
                project_id=record.id,
                dashboard_url=self.runtime.dashboard_url,
            )
            try:
                result = await tree.run(
                    command.argv, command.cwd, tree.child_env(command.env), timeout_s=source.timeout_s
                )
            except tree.ExecutableNotFoundError as error:
                raise MetricError(str(error)) from error
            if result.timed_out:
                raise MetricError(f"timed out after {source.timeout_s}s")
            if not result.ok:
                raise MetricError(f"exit {result.exit_code}: {result.stderr.strip()[-2000:]}")
            return _parse_reading(result.stdout)
        if isinstance(source, HttpSource):
            base = self._service_url(record.id, source.service)
            if base is None:
                raise MetricError(f"service {source.service!r} is not running")
            url = base.rstrip("/") + ("" if source.path.startswith("/") else "/") + source.path
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(url)
            except httpx.HTTPError as error:
                raise MetricError(f"GET {url} failed: {error}") from error
            if response.status_code != 200:
                raise MetricError(f"GET {url} answered {response.status_code}")
            return _parse_reading(response.text)
        assert isinstance(source, FileSource)
        path = record.path / source.path
        if not path.is_file():
            raise MetricError(f"{source.path} does not exist")
        return _parse_reading(path.read_text(encoding="utf-8"))

    async def _load_latest(self, project_id: str, metric: Metric) -> LatestReading:
        row = await self.runtime.db.fetch_one(
            "SELECT * FROM metric_readings WHERE project_id = ? AND metric_id = ? ORDER BY ts DESC LIMIT 1",
            (project_id, metric.id),
        )
        if row is None:
            return self._latest_from(metric, None, None, None)
        reading = (
            MetricReading(value=json.loads(row["value"]), status=row["status"], detail=row["detail"])
            if row["value"] is not None
            else None
        )
        return self._latest_from(metric, reading, row["error"], row["ts"])

    @staticmethod
    def _latest_from(
        metric: Metric, reading: MetricReading | None, error: str | None, at: float | None
    ) -> LatestReading:
        return LatestReading(
            metric_id=metric.id,
            label=metric.label,
            display=metric.display,
            unit=metric.unit,
            pinned=metric.pinned,
            value=reading.value if reading is not None else None,
            status=reading.status if reading is not None else None,
            detail=reading.detail if reading is not None else None,
            error=error,
            read_at=at,
            interval_s=metric.interval_s,
        )

    async def _sparkline(self, project_id: str, metric_id: str) -> list[float]:
        rows = await self.runtime.db.fetch_all(
            "SELECT value FROM metric_readings WHERE project_id = ? AND metric_id = ? AND value IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (project_id, metric_id, SPARKLINE_POINTS),
        )
        values = [json.loads(row["value"]) for row in reversed(rows)]
        return [float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)]

    async def _publish(self, project_id: str) -> None:
        latest = self._latest.get(project_id, {})
        record = self._projects.record(project_id)
        metrics = self._metrics(record)
        pinned = [
            PinnedMetric(
                id=metric.id,
                label=metric.label,
                display=metric.display,
                unit=metric.unit,
                value=latest[metric.id].value if metric.id in latest else None,
                status=latest[metric.id].status if metric.id in latest else None,
                history=await self._sparkline(project_id, metric.id),
                error=latest[metric.id].error if metric.id in latest else None,
            )
            for metric in metrics
            if metric.pinned
        ]
        self._projects.put(project_id, MetricsFacts(pinned=pinned))
        self._publish_state()

    def _publish_state(self) -> None:
        projects = [
            ProjectMetrics(
                project_id=project_id,
                metrics=list(readings.values()),
                interval_overrides=self._intervals.get(project_id, {}),
            )
            for project_id, readings in sorted(self._latest.items())
            if readings
        ]
        self.set_state(MetricsState(projects=projects))

    def _metric(self, project_id: str, metric_id: str) -> tuple[ProjectRecord, Metric]:
        record = self._projects.record(project_id)
        for metric in self._metrics(record):
            if metric.id == metric_id:
                return record, metric
        raise ActionError(f"no metric {metric_id!r} in the manifest", status=404)

    # --- actions ---

    @action(title="Refresh metrics")
    async def refresh(self, request: RefreshInput) -> ProjectMetrics:
        """Read a project's metrics now, without waiting for their interval."""
        record = self._projects.record(request.project_id)
        metrics = self._metrics(record)
        targets = [metric for metric in metrics if request.metric_id in (None, metric.id)]
        if request.metric_id is not None and not targets:
            raise ActionError(f"no metric {request.metric_id!r} in the manifest", status=404)
        await asyncio.gather(*(self.read(record, metric) for metric in targets))
        return ProjectMetrics(project_id=record.id, metrics=list(self._latest.get(record.id, {}).values()))

    @action(title="Set metric intervals")
    async def set_intervals(self, request: IntervalsInput) -> MetricIntervals:
        """Override how often this project's metrics are read; the manifest's interval_s stays the default."""
        record = self._projects.record(request.project_id)
        known = {metric.id for metric in record.manifest.metrics} if record.manifest is not None else set()
        unknown = sorted({item.metric_id for item in request.overrides} - known)
        if unknown:
            raise ActionError(f"no metric {', '.join(map(repr, unknown))} in the manifest", status=404)
        intervals = MetricIntervals(project_id=record.id, overrides=request.overrides)
        await self.runtime.db.kv_put(INTERVALS_NAMESPACE, record.id, intervals.model_dump(mode="json"))
        await self.project_upserted(record)
        return intervals

    @action(title="Metric history", query=True)
    async def history(self, request: HistoryInput) -> MetricHistory:
        """Readings oldest first, for history charts."""
        self._metric(request.project_id, request.metric_id)
        rows = await self.runtime.db.fetch_all(
            "SELECT * FROM metric_readings WHERE project_id = ? AND metric_id = ? ORDER BY ts DESC LIMIT ?",
            (request.project_id, request.metric_id, request.limit),
        )
        points = [
            MetricPoint(
                read_at=row["ts"],
                value=json.loads(row["value"]) if row["value"] is not None else None,
                status=row["status"],
                error=row["error"],
            )
            for row in reversed(rows)
        ]
        return MetricHistory(metric_id=request.metric_id, points=points)

    @action(title="Project metrics", query=True)
    async def project_metrics(self, request: ProjectRef) -> ProjectMetrics:
        """The latest reading of every metric of one project."""
        self._projects.record(request.project_id)
        return ProjectMetrics(
            project_id=request.project_id, metrics=list(self._latest.get(request.project_id, {}).values())
        )


def _parse_reading(text: str) -> MetricReading:
    try:
        return MetricReading.model_validate_json(text.strip())
    except ValidationError as error:
        raise MetricError(f"not a MetricReading: {error.errors()[0]['msg']} in {text.strip()[:200]!r}") from error
