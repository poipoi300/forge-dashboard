"""A restore stops and starts every service; the ones that run loops or supervise processes keep working."""

import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import MANIFEST
from testing_support import wait_for

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import NoInput
from forge_dashboard.domain.scheduling import IntervalSchedule
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.gallery import GalleryService
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.metrics import MetricsService
from forge_dashboard.services.project_services import ProjectServicesService, ServiceRef
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService
from forge_dashboard.services.settings import RestoreInput, SettingsService

PYTHON = sys.executable.replace(chr(92), "/")
WORKER_AND_METRIC = f"""
[[services]]
id = "worker"
label = "Worker"
argv = ["{PYTHON}", "-c", "import time; time.sleep(600)"]

[[metrics]]
id = "answer"
label = "Answer"
pinned = true
source = {{ kind = "command", argv = ["{PYTHON}", "scripts/answer.py"] }}
"""
ANSWER_SCRIPT = {"scripts/answer.py": 'import json\nprint(json.dumps({"value": 42}))\n'}


@dataclass
class Env:
    runtime: Runtime
    settings: SettingsService
    scheduler: SchedulerService
    metrics: MetricsService
    services: ProjectServicesService
    projects: ProjectsService
    discovery: DiscoveryService
    gallery: GalleryService


@pytest.fixture
async def env(data_paths: DataPaths, tmp_path: Path) -> AsyncIterator[Env]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    scheduler = SchedulerService(runtime, jobs, targets_ready=lambda: projects.state.loaded)
    services = ProjectServicesService(runtime, projects, jobs)
    metrics = MetricsService(runtime, projects, services.url_for)
    gallery = GalleryService(runtime, jobs)
    settings = SettingsService(runtime, projects)
    runtime.register([jobs, scheduler, manifests, projects, services, metrics, gallery, settings, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "no-projects-here")]})
    await runtime.start()
    await wait_for(lambda: projects.state.loaded)
    try:
        yield Env(runtime, settings, scheduler, metrics, services, projects, discovery, gallery)
    finally:
        await runtime.stop()


def _answer_read_at(metrics: MetricsService) -> float | None:
    return next(
        (
            reading.read_at
            for project in metrics.state.projects
            for reading in project.metrics
            if reading.metric_id == "answer" and reading.value == 42
        ),
        None,
    )


def _worker_is(env: Env, state: str) -> bool:
    return any(s.service_id == "worker" and s.state == state for s in env.services.state.services)


async def test_a_restore_leaves_metrics_the_scheduler_and_services_working(
    env: Env, make_project: Callable[..., Path]
) -> None:
    root = make_project("Svc", manifest=MANIFEST + WORKER_AND_METRIC, extra=ANSWER_SCRIPT)
    await env.discovery.add_project(PathInput(path=str(root)))
    await wait_for(lambda: _answer_read_at(env.metrics) is not None)
    read_before = _answer_read_at(env.metrics)
    assert read_before is not None
    await env.runtime.db.execute("DELETE FROM metric_readings")
    snapshot = await env.settings.backup(NoInput())  # knows the project, but holds no reading and no schedule
    await env.scheduler.upsert(
        entry_id="ping",
        owner="test",
        title="Ping",
        project_id=None,
        target_service="gallery",
        target_action="ping",
        payload={},
        schedule=IntervalSchedule(every_s=60),
    )
    worker = ServiceRef(project_id="svc", service_id="worker")
    await env.services.start_service(worker)
    await wait_for(lambda: _worker_is(env, "running"))

    await env.settings.restore(RestoreInput(name=snapshot.name))

    # Project services: the worker was stopped by the restore and can be started again.
    assert _worker_is(env, "stopped")
    await env.services.start_service(worker)
    await wait_for(lambda: _worker_is(env, "running"))
    await env.services.stop_service(worker)
    # Metrics: reloaded from the restored database and reading again.
    await wait_for(lambda: (_answer_read_at(env.metrics) or 0.0) > read_before)
    # Scheduler: reloaded from the restored database, and dispatching.
    assert env.scheduler.entry("ping") is None
    await env.scheduler.upsert(
        entry_id="ping-again",
        owner="test",
        title="Ping again",
        project_id=None,
        target_service="gallery",
        target_action="ping",
        payload={},
        schedule=IntervalSchedule(every_s=60),
    )
    assert await env.scheduler.tick(datetime.now(tz=UTC) + timedelta(hours=2)) == ["ping-again"]
    assert env.gallery.state.pings == 1
