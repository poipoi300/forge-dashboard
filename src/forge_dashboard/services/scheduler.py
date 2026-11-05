"""Persistent schedules that invoke service actions: fork syncs, scheduled checks.

Entries live in SQLite, so they survive restarts. A run missed while the
dashboard was down happens once on startup, never as a burst. Each run is
jittered, a global pause stops dispatching, and the upcoming queue is part
of the state. Concurrency limits belong to the jobs the actions submit.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, override

from pydantic import BaseModel, Field, JsonValue

from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, Done, JobRef, NoInput, Service, action
from forge_dashboard.core.tasks import BackgroundTasks
from forge_dashboard.core.ui import ui
from forge_dashboard.domain import scheduling
from forge_dashboard.domain.scheduling import ManualSchedule, Schedule
from forge_dashboard.services.jobs import JOB_STATUS_UI, JobsService, JobStatus, JobSummary

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

TICK_S = 1.0
QUEUE_SHOWN = 12


class ScheduleEntry(BaseModel):
    id: str = Field(json_schema_extra=ui(hidden=True))
    owner: str = Field(json_schema_extra=ui(hidden=True))
    title: str
    project_id: str | None = Field(None, json_schema_extra=ui(short_label="project"))
    target_service: str = Field(json_schema_extra=ui(hidden=True))
    target_action: str = Field(json_schema_extra=ui(hidden=True))
    payload: dict[str, JsonValue] = Field(default_factory=dict, json_schema_extra=ui(hidden=True))
    schedule: Schedule = Field(default_factory=ManualSchedule)
    max_jitter_s: int = Field(30, ge=0, json_schema_extra=ui(widget="duration", advanced=True))
    enabled: bool = True
    next_run_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="next"))
    last_run_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="last"))
    last_job_id: str | None = Field(None, json_schema_extra=ui(hidden=True))
    last_status: JobStatus | None = Field(None, json_schema_extra=JOB_STATUS_UI)


class SchedulerConfig(BaseModel):
    paused: bool = Field(False, json_schema_extra=ui(widget="toggle", help="Stop dispatching every schedule."))
    default_jitter_s: int = Field(30, ge=0, le=3600, json_schema_extra=ui(widget="duration", advanced=True))


class SchedulerState(BaseModel):
    paused: bool = False
    entries: list[ScheduleEntry] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["title", "project_id", "next_run_at", "last_run_at", "last_status"]),
    )
    queue: list[ScheduleEntry] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["title", "project_id", "next_run_at"], help="Upcoming runs, soonest first."),
    )


class EntryInput(BaseModel):
    entry_id: str = Field(json_schema_extra=ui(hidden=True))


class ScheduledRunFailed(Event):
    type: ClassVar[str] = "scheduler.run-failed"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "scheduled-failure"
    entry_id: str
    entry_title: str
    job_id: str | None
    detail: str

    def title(self) -> str:
        return f"Scheduled run failed: {self.entry_title}"

    def severity(self) -> Severity:
        return "error"


def _now() -> datetime:
    return datetime.now(tz=UTC).astimezone()


def _stamp(moment: datetime | None) -> float | None:
    return moment.timestamp() if moment is not None else None


class SchedulerService(Service[SchedulerConfig, SchedulerState]):
    id = "scheduler"
    title = "Scheduler"
    description = "Persistent schedules for fork syncs and checks, with catch-up, jitter, and a global pause."
    Config = SchedulerConfig
    State = SchedulerState
    events = (ScheduledRunFailed,)

    def __init__(self, runtime: Runtime, jobs: JobsService) -> None:
        super().__init__(runtime)
        self._jobs = jobs
        self._entries: dict[str, ScheduleEntry] = {}
        self._task: asyncio.Task[None] | None = None
        self._random = random.Random()
        self._background = BackgroundTasks()

    @override
    async def start(self) -> None:
        rows = await self.runtime.db.fetch_all("SELECT entry FROM schedules")
        now = _now()
        for row in rows:
            entry = ScheduleEntry.model_validate_json(row["entry"])
            next_at = datetime.fromtimestamp(entry.next_run_at, tz=UTC) if entry.next_run_at is not None else None
            caught_up = scheduling.catch_up(next_at, now)
            self._entries[entry.id] = entry.model_copy(update={"next_run_at": _stamp(caught_up)})
        self._jobs.on_finished(self._job_finished)
        self._publish_state()
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    @override
    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._background.cancel_all()

    @override
    async def on_config_changed(self, previous: SchedulerConfig) -> None:
        self._publish_state()

    # --- API for other services ---

    async def upsert(
        self,
        *,
        entry_id: str,
        owner: str,
        title: str,
        project_id: str | None,
        target_service: str,
        target_action: str,
        payload: dict[str, JsonValue],
        schedule: Schedule,
        enabled: bool = True,
    ) -> ScheduleEntry:
        """Create or replace an entry; the next run is recomputed from now."""
        existing = self._entries.get(entry_id)
        entry = ScheduleEntry(
            id=entry_id,
            owner=owner,
            title=title,
            project_id=project_id,
            target_service=target_service,
            target_action=target_action,
            payload=payload,
            schedule=schedule,
            max_jitter_s=self.config.default_jitter_s,
            enabled=enabled,
            last_run_at=existing.last_run_at if existing else None,
            last_job_id=existing.last_job_id if existing else None,
            last_status=existing.last_status if existing else None,
        )
        entry = entry.model_copy(update={"next_run_at": self._next_for(entry, _now())})
        await self._save(entry)
        return entry

    async def remove(self, entry_id: str) -> None:
        self._entries.pop(entry_id, None)
        await self.runtime.db.execute("DELETE FROM schedules WHERE id = ?", (entry_id,))
        self._publish_state()

    def entry(self, entry_id: str) -> ScheduleEntry | None:
        return self._entries.get(entry_id)

    # --- loop ---

    def _next_for(self, entry: ScheduleEntry, after: datetime) -> float | None:
        if not entry.enabled:
            return None
        jitter = scheduling.draw_jitter(entry.max_jitter_s, self._random.uniform)
        return _stamp(scheduling.next_run_after(entry.schedule, after, jitter))

    async def _loop(self) -> None:
        while True:
            await self.tick(_now())
            await asyncio.sleep(TICK_S)

    async def tick(self, now: datetime) -> list[str]:
        """Dispatch every due entry; returns the ids dispatched."""
        candidates = [
            (entry.id, datetime.fromtimestamp(entry.next_run_at, tz=UTC) if entry.next_run_at is not None else None)
            for entry in self._entries.values()
            if entry.enabled
        ]
        dispatched: list[str] = []
        for entry_id in scheduling.due(candidates, now, self.config.paused):
            await self._dispatch(self._entries[entry_id], now)
            dispatched.append(entry_id)
        return dispatched

    async def _dispatch(self, entry: ScheduleEntry, now: datetime) -> None:
        job_id: str | None = None
        status: JobStatus | None = JobStatus.QUEUED
        try:
            result = await self.runtime.invoke(entry.target_service, entry.target_action, entry.payload)
            job_id = result.job_id if isinstance(result, JobRef) else None
            if job_id is None:
                status = JobStatus.SUCCEEDED
        except ActionError as error:
            status = JobStatus.FAILED
            self.publish(
                ScheduledRunFailed(
                    project_id=entry.project_id,
                    entry_id=entry.id,
                    entry_title=entry.title,
                    job_id=None,
                    detail=error.message,
                )
            )
        updated = entry.model_copy(
            update={
                "last_run_at": now.timestamp(),
                "last_job_id": job_id,
                "last_status": status,
                "next_run_at": self._next_for(entry, now),
            }
        )
        await self._save(updated)

    def _job_finished(self, summary: JobSummary) -> None:
        for entry in self._entries.values():
            if entry.last_job_id != summary.id:
                continue
            self._entries[entry.id] = entry.model_copy(update={"last_status": summary.status})
            self._background.spawn(self._save(self._entries[entry.id]), name=f"scheduler-save:{entry.id}")
            if summary.status in (JobStatus.FAILED, JobStatus.ATTENTION):
                self.publish(
                    ScheduledRunFailed(
                        project_id=entry.project_id,
                        entry_id=entry.id,
                        entry_title=entry.title,
                        job_id=summary.id,
                        detail=summary.error or summary.summary or summary.status.value,
                    )
                )

    async def _save(self, entry: ScheduleEntry) -> None:
        self._entries[entry.id] = entry
        await self.runtime.db.execute(
            "INSERT INTO schedules (id, entry) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET entry = excluded.entry",
            (entry.id, entry.model_dump_json()),
        )
        self._publish_state()

    def _publish_state(self) -> None:
        entries = sorted(self._entries.values(), key=lambda e: (e.next_run_at is None, e.next_run_at or 0, e.title))
        queue = [entry for entry in entries if entry.enabled and entry.next_run_at is not None][:QUEUE_SHOWN]
        self.set_state(SchedulerState(paused=self.config.paused, entries=entries, queue=queue))

    # --- actions ---

    @action(title="Pause all schedules")
    async def pause(self, _request: NoInput) -> Done:
        """Stop dispatching scheduled runs until resumed."""
        await self.update_config(self.config.model_copy(update={"paused": True}))
        return Done(message="paused")

    @action(title="Resume schedules")
    async def resume(self, _request: NoInput) -> Done:
        """Dispatch scheduled runs again; missed ones run once."""
        await self.update_config(self.config.model_copy(update={"paused": False}))
        return Done(message="resumed")

    @action(title="Run now")
    async def run_now(self, request: EntryInput) -> Done:
        """Dispatch one entry immediately, keeping its schedule."""
        entry = self._entries.get(request.entry_id)
        if entry is None:
            raise ActionError(f"no schedule {request.entry_id!r}", status=404)
        await self._dispatch(entry, _now())
        return Done(message="dispatched")

    @action(title="Skip next run")
    async def skip_next(self, request: EntryInput) -> Done:
        """Move one entry's next run to the slot after it."""
        entry = self._entries.get(request.entry_id)
        if entry is None or entry.next_run_at is None:
            raise ActionError("nothing scheduled to skip", status=409)
        after = datetime.fromtimestamp(entry.next_run_at, tz=UTC).astimezone() + timedelta(seconds=1)
        await self._save(entry.model_copy(update={"next_run_at": self._next_for(entry, after)}))
        return Done(message="skipped")
