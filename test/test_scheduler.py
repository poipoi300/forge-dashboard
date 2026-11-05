"""Scheduling math and the persistent scheduler service."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import NoInput
from forge_dashboard.domain import scheduling
from forge_dashboard.domain.scheduling import CronSchedule, IntervalSchedule, ManualSchedule
from forge_dashboard.services.gallery import GalleryService
from forge_dashboard.services.jobs import JobsService, JobStatus
from forge_dashboard.services.scheduler import EntryInput, SchedulerService

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
