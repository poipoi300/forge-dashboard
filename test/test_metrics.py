"""Project metrics: sources, thresholds, failures, history, and pinned facts."""

import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import respx
from conftest import MANIFEST
from testing_support import wait_for

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, ProjectRef
from forge_dashboard.manifest import MetricReading
from forge_dashboard.manifest.models import MetricThresholds
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.metrics import HistoryInput, MetricsService, RefreshInput, apply_thresholds
from forge_dashboard.services.projects import ProjectsService

PYTHON = sys.executable.replace(chr(92), "/")
METRIC_SCRIPT = {"scripts/metric_files.py": 'import json\nprint(json.dumps({"value": 3}))\n'}


def test_reading_status_wins_over_thresholds() -> None:
    thresholds = MetricThresholds(fail_below=10)
    reading = MetricReading(value=1, status="ok")
    assert apply_thresholds(reading, thresholds).status == "ok"


@pytest.mark.parametrize(
    ("value", "status"),
    [(5, "fail"), (15, "warn"), (50, "ok"), (95, "warn"), (120, "fail")],
)
def test_thresholds_set_status_with_fail_bounds_first(value: int, status: str) -> None:
    thresholds = MetricThresholds(fail_below=10, warn_below=20, warn_above=90, fail_above=100)
    assert apply_thresholds(MetricReading(value=value), thresholds).status == status


def test_non_numeric_readings_and_missing_thresholds_keep_no_status() -> None:
    assert apply_thresholds(MetricReading(value="text"), MetricThresholds(fail_below=1)).status is None
    assert apply_thresholds(MetricReading(value=True), MetricThresholds(fail_below=1)).status is None
    assert apply_thresholds(MetricReading(value=3), None).status is None
    assert apply_thresholds(MetricReading(value=3), MetricThresholds()).status is None


METRICS = f"""
[[services]]
id = "app"
label = "App"
argv = ["{PYTHON}", "-c", "print(1)", "{{port}}"]
port = {{ policy = "dynamic" }}

[[metrics]]
id = "files"
label = "Files"
unit = "files"
pinned = true
thresholds = {{ warn_below = 5 }}
source = {{ kind = "command", argv = ["{PYTHON}", "scripts/metric_files.py"] }}

[[metrics]]
id = "report"
label = "Report"
display = "percent"
source = {{ kind = "file", path = "reports/metric.json" }}

[[metrics]]
id = "live"
label = "Live"
source = {{ kind = "http", service = "app", path = "/metrics/live" }}

[[metrics]]
id = "broken"
label = "Broken"
source = {{ kind = "command", argv = ["{PYTHON}", "-c", "import sys; print('nope', file=sys.stderr); sys.exit(2)"] }}
"""


@pytest.fixture
async def metrics_env(
    data_paths: DataPaths, tmp_path: Path
) -> AsyncIterator[tuple[MetricsService, ProjectsService, DiscoveryService, dict[str, str]]]:
    urls: dict[str, str] = {}
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    metrics = MetricsService(runtime, projects, lambda project_id, service_id: urls.get(f"{project_id}:{service_id}"))
    runtime.register([jobs, manifests, projects, metrics, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")]})
    await runtime.start()
    try:
        yield metrics, projects, discovery, urls
    finally:
        await runtime.stop()


async def test_every_source_kind_is_read_and_failures_show_stderr(
    metrics_env: tuple[MetricsService, ProjectsService, DiscoveryService, dict[str, str]],
    make_project: Callable[..., Path],
) -> None:
    metrics, projects, discovery, urls = metrics_env
    extra = {**METRIC_SCRIPT, "reports/metric.json": '{"value": 87.5, "detail": "from file"}'}
    alpha = make_project("Alpha", manifest=MANIFEST + METRICS, extra=extra)
    await discovery.add_project(PathInput(path=str(alpha)))
    await wait_for(lambda: any(p.project_id == "alpha" for p in metrics.state.projects))
    urls["alpha:app"] = "http://127.0.0.1:9999"
    with respx.mock(assert_all_called=False) as router:
        router.get("http://127.0.0.1:9999/metrics/live").mock(return_value=httpx.Response(200, json={"value": "up"}))
        refreshed = await metrics.refresh(RefreshInput(project_id="alpha"))
    by_id = {reading.metric_id: reading for reading in refreshed.metrics}
    assert (by_id["files"].value, by_id["files"].status) == (3, "warn")
    assert (by_id["report"].value, by_id["report"].detail) == (87.5, "from file")
    assert by_id["live"].value == "up"
    assert by_id["broken"].value is None and "exit 2: nope" in (by_id["broken"].error or "")
    pinned = projects.summary("alpha").metrics
    # The interval loop reads once on its own at startup, so history holds that reading plus the refresh.
    assert [(m.id, m.value, m.status) for m in pinned] == [("files", 3, "warn")]
    assert pinned[0].history and set(pinned[0].history) == {3.0}


async def test_stopped_services_and_bad_readings_are_reported(
    metrics_env: tuple[MetricsService, ProjectsService, DiscoveryService, dict[str, str]],
    make_project: Callable[..., Path],
) -> None:
    metrics, _projects, discovery, _urls = metrics_env
    extra = {**METRIC_SCRIPT, "reports/metric.json": '{"value": [1, 2]}'}
    alpha = make_project("Alpha", manifest=MANIFEST + METRICS, extra=extra)
    await discovery.add_project(PathInput(path=str(alpha)))
    await wait_for(lambda: any(p.project_id == "alpha" for p in metrics.state.projects))
    live = await metrics.refresh(RefreshInput(project_id="alpha", metric_id="live"))
    assert next(m for m in live.metrics if m.metric_id == "live").error == "service 'app' is not running"
    report = await metrics.refresh(RefreshInput(project_id="alpha", metric_id="report"))
    assert "not a MetricReading" in (next(m for m in report.metrics if m.metric_id == "report").error or "")
    (alpha / "reports" / "metric.json").unlink()
    gone = await metrics.refresh(RefreshInput(project_id="alpha", metric_id="report"))
    assert "does not exist" in (next(m for m in gone.metrics if m.metric_id == "report").error or "")
    with pytest.raises(ActionError, match="no metric"):
        await metrics.refresh(RefreshInput(project_id="alpha", metric_id="ghost"))


async def test_history_accumulates_oldest_first(
    metrics_env: tuple[MetricsService, ProjectsService, DiscoveryService, dict[str, str]],
    make_project: Callable[..., Path],
) -> None:
    metrics, projects, discovery, _urls = metrics_env
    alpha = make_project("Alpha", manifest=MANIFEST + METRICS, extra=METRIC_SCRIPT)
    await discovery.add_project(PathInput(path=str(alpha)))
    await wait_for(lambda: any(p.project_id == "alpha" for p in metrics.state.projects))
    for _ in range(3):
        await metrics.refresh(RefreshInput(project_id="alpha", metric_id="files"))
    history = await metrics.history(HistoryInput(project_id="alpha", metric_id="files"))
    assert len(history.points) >= 3 and all(point.value == 3 for point in history.points)
    assert history.points == sorted(history.points, key=lambda point: point.read_at)
    latest = await metrics.project_metrics(ProjectRef(project_id="alpha"))
    assert {reading.metric_id for reading in latest.metrics} == {"files", "report", "live", "broken"}
    assert len(projects.summary("alpha").metrics[0].history) >= 3
