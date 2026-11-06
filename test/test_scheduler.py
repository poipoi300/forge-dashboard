"""Scheduling math and the persistent scheduler service."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue, ValidationError
from testing_support import wait_for

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import Done, JobRef, NoInput, Service, action
from forge_dashboard.domain import scheduling
from forge_dashboard.domain.scheduling import CronSchedule, IntervalSchedule, ManualSchedule
from forge_dashboard.services import scheduler as scheduler_module
from forge_dashboard.services.discovery import DiscoveryService
from forge_dashboard.services.gallery import GalleryService
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import EntryInput, ScheduleEntry, SchedulerService

NOON = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def test_interval_runs_after_the_interval_plus_jitter() -> None:
    upcoming = scheduling.next_run_after(IntervalSchedule(every_s=3600), NOON, timedelta(seconds=7))
    assert upcoming == NOON + timedelta(hours=1, seconds=7)


def test_cron_uses_the_next_matching_slot() -> None:
    assert scheduling.next_run_after(CronSchedule(expression="30 6 * * *"), NOON) == datetime(
        2026, 11, 6, 6, 30, tzinfo=UTC
    )


def test_manual_schedules_never_run_on_their_own() -> None:
    assert scheduling.next_run_after(ManualSchedule(), NOON) is None


def test_invalid_cron_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid cron"):
        CronSchedule(expression="every tuesday")


def test_missed_runs_catch_up_once_at_startup() -> None:
    missed = NOON - timedelta(days=3)
    assert scheduling.catch_up(missed, NOON) == NOON
    assert scheduling.catch_up(NOON + timedelta(hours=1), NOON) == NOON + timedelta(hours=1)
    assert scheduling.catch_up(None, NOON) is None


def test_due_orders_oldest_first_and_honours_pause() -> None:
    entries = [("b", NOON - timedelta(minutes=1)), ("a", NOON - timedelta(minutes=5)), ("c", NOON + timedelta(1))]
    assert scheduling.due(entries, NOON, paused=False) == ["a", "b"]
    assert scheduling.due(entries, NOON, paused=True) == []


def test_jitter_draws_from_the_injected_source() -> None:
    assert scheduling.draw_jitter(30, lambda low, high: high) == timedelta(seconds=30)
    assert scheduling.draw_jitter(0, lambda low, high: high) == timedelta(0)


def _services(runtime: Runtime) -> tuple[SchedulerService, JobsService, GalleryService]:
    scheduler, jobs, gallery = runtime.services["scheduler"], runtime.services["jobs"], runtime.services["gallery"]
    assert isinstance(scheduler, SchedulerService)
    assert isinstance(jobs, JobsService)
    assert isinstance(gallery, GalleryService)
    return scheduler, jobs, gallery


async def test_due_entries_invoke_their_action_and_record_the_job(started_runtime: Runtime) -> None:
    scheduler, jobs, _gallery = _services(started_runtime)
    entry = await scheduler.upsert(
        entry_id="count",
        owner="test",
        title="Count",
        project_id=None,
        target_service="gallery",
        target_action="count-slowly",
        payload={},
        schedule=IntervalSchedule(every_s=60),
    )
    assert entry.next_run_at is not None
    assert scheduler.state.queue[0].id == "count"
    now = datetime.fromtimestamp(entry.next_run_at + 1, tz=UTC)
    assert await scheduler.tick(now) == ["count"]
    dispatched = scheduler.entry("count")
    assert dispatched is not None and dispatched.last_job_id is not None
    final = await jobs.wait(dispatched.last_job_id)
    assert final.status is JobStatus.SUCCEEDED
    assert dispatched.next_run_at is not None and dispatched.next_run_at > now.timestamp()


async def test_pause_stops_dispatch_and_failures_are_published(started_runtime: Runtime) -> None:
    scheduler, _jobs, gallery = _services(started_runtime)
    await scheduler.upsert(
        entry_id="fail",
        owner="test",
        title="Always fails",
        project_id="demo",
        target_service="gallery",
        target_action="fail",
        payload={},
        schedule=IntervalSchedule(every_s=60),
    )
    later = datetime.now(tz=UTC) + timedelta(hours=2)
    await scheduler.pause(NoInput())
    assert await scheduler.tick(later) == []
    await scheduler.resume(NoInput())
    async with started_runtime.bus.subscribe() as events:
        assert await scheduler.tick(later) == ["fail"]
        envelope = events.get_nowait()
    assert envelope.type == "scheduler.run-failed" and envelope.notify_kind == "scheduled-failure"
    entry = scheduler.entry("fail")
    assert entry is not None and entry.last_status is JobStatus.FAILED
    assert gallery.state.pings == 0


async def test_entries_survive_restart_and_missed_runs_fire_once(make_runtime: Callable[[], Runtime]) -> None:
    runtime = make_runtime()
    await runtime.start()
    scheduler, _jobs, _gallery = _services(runtime)
    await scheduler.upsert(
        entry_id="ping",
        owner="test",
        title="Ping",
        project_id=None,
        target_service="gallery",
        target_action="ping",
        payload={},
        schedule=IntervalSchedule(every_s=60),
    )
    stored = scheduler.entry("ping")
    assert stored is not None
    await runtime.db.execute(
        "UPDATE schedules SET entry = ? WHERE id = 'ping'",
        (stored.model_copy(update={"next_run_at": NOON.timestamp() - 86400}).model_dump_json(),),
    )
    await runtime.stop()

    restarted = make_runtime()
    await restarted.start()
    try:
        scheduler, _jobs, gallery = _services(restarted)
        reloaded = scheduler.entry("ping")
        assert reloaded is not None and reloaded.next_run_at is not None
        now = datetime.now(tz=UTC)
        assert reloaded.next_run_at <= now.timestamp()
        assert await scheduler.tick(now) == ["ping"]
        assert await scheduler.tick(now) == []
        assert gallery.state.pings == 1
        await scheduler.skip_next(EntryInput(entry_id="ping"))
        await scheduler.run_now(EntryInput(entry_id="ping"))
        assert gallery.state.pings == 2
        await scheduler.remove("ping")
        assert scheduler.entry("ping") is None
    finally:
        await restarted.stop()


# --- dispatch waits for the services entries target ---


class Empty(BaseModel):
    pass


class Observer(Service[Empty, Empty]):
    """Records whether the project registry had loaded when an entry reached it."""

    id = "observer"
    title = "Observer"
    description = "Test target for scheduled entries."
    Config = Empty
    State = Empty

    def __init__(self, runtime: Runtime, projects: ProjectsService) -> None:
        super().__init__(runtime)
        self._projects = projects
        self.registry_loaded_at_dispatch: list[bool] = []

    @action(title="Observe")
    async def observe(self, _request: NoInput) -> Done:
        self.registry_loaded_at_dispatch.append(self._projects.state.loaded)
        return Done()


class JobMaker(Service[Empty, Empty]):
    """Target of an entry whose action submits a job that fails."""

    id = "jobmaker"
    title = "Job maker"
    description = "Test target for scheduled entries."
    Config = Empty
    State = Empty

    def __init__(self, runtime: Runtime, jobs: JobsService) -> None:
        super().__init__(runtime)
        self._jobs = jobs

    @action(title="Fail in a job", long_running=True)
    async def fail_in_a_job(self, _request: NoInput) -> JobRef:
        async def work(context: JobContext) -> JobOutcome:
            return JobOutcome(status=JobStatus.FAILED, summary="the job failed")

        return await self._jobs.submit(kind="test.fail", title="Failing job", work=work)


async def test_nothing_is_dispatched_until_the_targets_are_ready(data_paths: DataPaths) -> None:
    ready = False
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs, targets_ready=lambda: ready)
    gallery = GalleryService(runtime, jobs)
    runtime.register([jobs, scheduler, gallery])
    await runtime.start()
    try:
        await scheduler.upsert(
            entry_id="ping",
            owner="test",
            title="Ping",
            project_id=None,
            target_service="gallery",
            target_action="ping",
            payload={},
            schedule=IntervalSchedule(every_s=60),
        )
        later = datetime.now(tz=UTC) + timedelta(hours=2)
        assert await scheduler.tick(later) == []
        assert gallery.state.pings == 0
        ready = True
        assert await scheduler.tick(later) == ["ping"]
        assert gallery.state.pings == 1
    finally:
        await runtime.stop()


async def test_a_missed_run_waits_for_the_project_registry_to_load_then_runs_once(
    data_paths: DataPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "TICK_S", 0.05)
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    scheduler = SchedulerService(runtime, jobs, targets_ready=lambda: projects.state.loaded)
    observer = Observer(runtime, projects)
    runtime.register([jobs, scheduler, manifests, projects, observer, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "no-projects-here")]})
    missed = ScheduleEntry(
        id="missed",
        owner="test",
        title="Missed while the dashboard was down",
        target_service="observer",
        target_action="observe",
        schedule=IntervalSchedule(every_s=3600),
        next_run_at=NOON.timestamp() - 86400,
    )
    await runtime.db.execute("INSERT INTO schedules (id, entry) VALUES (?, ?)", (missed.id, missed.model_dump_json()))
    await runtime.start()
    try:
        await wait_for(lambda: observer.registry_loaded_at_dispatch != [])
        await asyncio.sleep(0.3)  # more ticks pass; the missed run does not repeat
        assert observer.registry_loaded_at_dispatch == [True]
        entry = scheduler.entry("missed")
        assert entry is not None and entry.last_status is JobStatus.SUCCEEDED
    finally:
        await runtime.stop()


# --- an entry removed, replaced, or crashing while it is dispatched ---


async def _upsert(
    scheduler: SchedulerService, entry_id: str, *, title: str | None = None, payload: dict[str, JsonValue] | None = None
) -> ScheduleEntry:
    return await scheduler.upsert(
        entry_id=entry_id,
        owner="test",
        title=title or entry_id,
        project_id=None,
        target_service="gallery",
        target_action="ping",
        payload=payload or {},
        schedule=IntervalSchedule(every_s=60),
    )


async def test_removing_another_entry_while_one_dispatches_does_not_break_the_tick(
    started_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, _jobs, _gallery = _services(started_runtime)
    for name in ("a", "b"):
        await _upsert(scheduler, name, payload={"which": name})

    async def invoke(service_id: str, action_id: str, payload: dict[str, JsonValue]) -> BaseModel:
        await scheduler.remove("b" if payload["which"] == "a" else "a")
        return Done()

    monkeypatch.setattr(started_runtime, "invoke", invoke)
    dispatched = await scheduler.tick(datetime.now(tz=UTC) + timedelta(hours=2))

    assert len(dispatched) == 1
    assert [entry.id for entry in scheduler.state.entries] == dispatched


async def test_an_entry_removed_while_its_own_run_dispatches_is_not_resurrected(
    started_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, _jobs, _gallery = _services(started_runtime)
    await _upsert(scheduler, "doomed")

    async def invoke(service_id: str, action_id: str, payload: dict[str, JsonValue]) -> BaseModel:
        await scheduler.remove("doomed")
        return Done()

    monkeypatch.setattr(started_runtime, "invoke", invoke)
    assert await scheduler.tick(datetime.now(tz=UTC) + timedelta(hours=2)) == ["doomed"]

    assert scheduler.entry("doomed") is None and scheduler.state.entries == []
    assert await started_runtime.db.fetch_all("SELECT id FROM schedules") == []


async def test_an_entry_replaced_while_its_run_dispatches_keeps_its_new_definition(
    started_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, _jobs, _gallery = _services(started_runtime)
    await _upsert(scheduler, "edited")

    async def invoke(service_id: str, action_id: str, payload: dict[str, JsonValue]) -> BaseModel:
        await _upsert(scheduler, "edited", title="Edited while running")
        return Done()

    monkeypatch.setattr(started_runtime, "invoke", invoke)
    await scheduler.tick(datetime.now(tz=UTC) + timedelta(hours=2))

    entry = scheduler.entry("edited")
    assert entry is not None and entry.title == "Edited while running" and entry.last_status is JobStatus.SUCCEEDED


async def test_an_action_that_crashes_is_a_failed_run_and_the_schedule_goes_on(
    started_runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler, _jobs, _gallery = _services(started_runtime)
    await _upsert(scheduler, "crashy")

    async def invoke(service_id: str, action_id: str, payload: dict[str, JsonValue]) -> BaseModel:
        raise RuntimeError("boom")

    monkeypatch.setattr(started_runtime, "invoke", invoke)
    later = datetime.now(tz=UTC) + timedelta(hours=2)
    async with started_runtime.bus.subscribe() as events:
        assert await scheduler.tick(later) == ["crashy"]
        envelope = events.get_nowait()

    assert envelope.type == "scheduler.run-failed" and envelope.data["detail"] == "RuntimeError: boom"
    entry = scheduler.entry("crashy")
    assert entry is not None and entry.last_status is JobStatus.FAILED
    assert entry.next_run_at is not None and entry.next_run_at > later.timestamp()
    assert "scheduled run crashy crashed" in caplog.text


async def test_the_dispatch_loop_survives_a_tick_that_raises(
    make_runtime: Callable[[], Runtime], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(scheduler_module, "TICK_S", 0.02)
    runtime = make_runtime()
    scheduler, _jobs, _gallery = _services(runtime)
    ticks: list[datetime] = []

    async def flaky(now: datetime) -> list[str]:
        ticks.append(now)
        if len(ticks) == 1:
            raise RuntimeError("tick broke")
        return []

    monkeypatch.setattr(scheduler, "tick", flaky)
    await runtime.start()
    try:
        await wait_for(lambda: len(ticks) >= 3)
    finally:
        await runtime.stop()
    assert "scheduler tick failed" in caplog.text


# --- stop and start are symmetric ---


async def test_stopping_and_starting_the_scheduler_reloads_entries_and_reports_a_failed_job_once(
    make_runtime: Callable[[], Runtime],
) -> None:
    runtime = make_runtime()
    scheduler, jobs, _gallery = _services(runtime)
    runtime.register([JobMaker(runtime, jobs)])
    await runtime.start()
    try:
        await scheduler.upsert(
            entry_id="failing-job",
            owner="test",
            title="Failing job",
            project_id=None,
            target_service="jobmaker",
            target_action="fail-in-a-job",
            payload={},
            schedule=IntervalSchedule(every_s=60),
        )
        await scheduler.stop()
        assert scheduler.entry("failing-job") is None
        await scheduler.start()
        assert scheduler.entry("failing-job") is not None

        async with runtime.bus.subscribe() as events:
            assert await scheduler.tick(datetime.now(tz=UTC) + timedelta(hours=2)) == ["failing-job"]
            dispatched = scheduler.entry("failing-job")
            assert dispatched is not None and dispatched.last_job_id is not None
            await jobs.wait(dispatched.last_job_id)
            await asyncio.sleep(0.2)
            failures = []
            while not events.empty():
                envelope = events.get_nowait()
                if envelope.type == "scheduler.run-failed":
                    failures.append(envelope)
        assert len(failures) == 1
    finally:
        await runtime.stop()
