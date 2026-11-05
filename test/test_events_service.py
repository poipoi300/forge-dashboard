"""The event log, notification center, and desktop notifications."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest
from desktop_notifier import Urgency
from pydantic import BaseModel
from testing_support import wait_for

from forge_dashboard.container import build_runtime
from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, Service
from forge_dashboard.services import events as events_module
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.events import (
    NOTIFICATION_KINDS,
    ActivityInput,
    EventsConfig,
    EventsService,
    MarkReadInput,
    NotificationsInput,
)
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService

NOW = time.time()


class Noted(Event):
    type: ClassVar[str] = "test.noted"
    persist: ClassVar[bool] = True
    note: str = "note"
    key: str | None = None
    at: float | None = None

    def title(self) -> str:
        return f"Noted {self.note}"

    def dedupe_key(self) -> str | None:
        return self.key

    def occurred_at(self) -> float | None:
        return self.at


class Transient(Event):
    type: ClassVar[str] = "test.transient"


class Settled(Event):
    """Published last so a test can wait for everything before it to be recorded."""

    type: ClassVar[str] = "test.settled"
    persist: ClassVar[bool] = True
    key: str

    def dedupe_key(self) -> str:
        return self.key


class CiBroke(Event):
    type: ClassVar[str] = "test.ci-broke"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "ci-failure"
    key: str | None = None
    at: float | None = None

    def title(self) -> str:
        return "CI failed on main"

    def severity(self) -> Severity:
        return "error"

    def dedupe_key(self) -> str | None:
        return self.key

    def occurred_at(self) -> float | None:
        return self.at


class ForkLanded(Event):
    type: ClassVar[str] = "test.fork-landed"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "fork-success"

    def title(self) -> str:
        return "Fork synced"

    def severity(self) -> Severity:
        return "success"


class ForkFailed(Event):
    type: ClassVar[str] = "test.fork-failed"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "fork-failure"
    loud: bool = True

    def title(self) -> str:
        return "Fork sync failed"

    def should_notify(self) -> bool:
        return self.loud


class Unlisted(Event):
    type: ClassVar[str] = "test.unlisted"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "no-such-kind"


class Empty(BaseModel):
    pass


class Emitter(Service[Empty, Empty]):
    id = "emitter"
    title = "Emitter"
    description = "Declares the events these tests publish."
    Config = Empty
    State = Empty
    events = (Noted, Transient, Settled, CiBroke, ForkLanded, ForkFailed, Unlisted)


class FakeNotifier:
    """Records what would have been shown; never touches the OS."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Urgency]] = []
        self.failure: Exception | None = None

    async def send(self, title: str, message: str, urgency: Urgency) -> str:
        if self.failure is not None:
            raise self.failure
        self.sent.append((title, message, urgency))
        return "fake-notification"


@dataclass
class Env:
    runtime: Runtime
    events: EventsService
    emitter: Emitter
    projects: ProjectsService
    discovery: DiscoveryService
    notifier: FakeNotifier

    async def settle(self) -> None:
        """Wait until the recorder has handled every event published so far (the queue is first-in first-out)."""
        key = uuid.uuid4().hex
        self.emitter.publish(Settled(project_id="settle", key=key))
        async with asyncio.timeout(10):
            while await self.runtime.db.fetch_one("SELECT 1 FROM events WHERE dedupe_key = ?", (key,)) is None:
                await asyncio.sleep(0.01)

    async def activity(self, **filters: object) -> list[str]:
        """Titles of the events matching `filters`, newest first."""
        page = await self.events.activity(ActivityInput.model_validate(filters))
        return [entry.title for entry in page.entries]


@asynccontextmanager
async def running(data_paths: DataPaths, *, inject_notifier: bool = True) -> AsyncIterator[Env]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    notifier = FakeNotifier()
    events = EventsService(runtime, projects, notifier=notifier if inject_notifier else None)
    emitter = Emitter(runtime)
    runtime.register([events, emitter, jobs, manifests, projects, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(data_paths.root.parent / "no-projects-here")]})
    await runtime.start()
    try:
        yield Env(runtime, events, emitter, projects, discovery, notifier)
    finally:
        await runtime.stop()


@pytest.fixture
async def env(data_paths: DataPaths) -> AsyncIterator[Env]:
    async with running(data_paths) as environment:
        yield environment


async def test_persisted_events_are_logged_and_transient_ones_are_not(env: Env) -> None:
    env.emitter.publish(Noted(project_id="alpha", note="one"))
    env.emitter.publish(Transient(project_id="alpha"))
    await env.settle()

    assert await env.activity(project_id="alpha") == ["Noted one"]
    row = await env.runtime.db.fetch_one("SELECT * FROM events WHERE type = 'test.noted'")
    assert row is not None
    assert (row["service"], row["project_id"], row["severity"], row["notify"], row["read"]) == (
        "emitter",
        "alpha",
        "info",
        0,
        0,
    )
    assert json.loads(row["data"]) == {"project_id": "alpha", "note": "one", "key": None, "at": None}


async def test_repeat_observations_with_one_dedupe_key_are_stored_once(env: Env) -> None:
    for _ in range(3):
        env.emitter.publish(Noted(project_id="alpha", key="commit:alpha:abc"))
    env.emitter.publish(Noted(project_id="alpha", key="commit:alpha:def"))
    await env.settle()

    assert len(await env.activity(project_id="alpha")) == 2


async def test_activity_is_newest_first_and_filters_by_project_service_and_type(env: Env) -> None:
    for index, project_id in enumerate(["alpha", "beta", "alpha"]):
        env.emitter.publish(Noted(project_id=project_id, note=str(index), at=NOW - 100 + index))
    env.emitter.publish(CiBroke(project_id="alpha", at=NOW - 50))
    await env.settle()

    assert await env.activity(project_id="alpha") == ["CI failed on main", "Noted 2", "Noted 0"]
    assert await env.activity(project_id="beta") == ["Noted 1"]
    assert await env.activity(project_id="alpha", types=["test.noted"]) == ["Noted 2", "Noted 0"]
    assert await env.activity(project_id="alpha", types=["test.noted", "test.ci-broke"], services=["jobs"]) == []
    assert await env.activity(project_id="alpha", services=["emitter"], limit=1) == ["CI failed on main"]


async def test_activity_entries_carry_the_event_document(env: Env) -> None:
    env.emitter.publish(CiBroke(project_id="alpha", at=NOW - 10))
    await env.settle()

    [entry] = (await env.events.activity(ActivityInput(project_id="alpha"))).entries
    assert (entry.service, entry.type, entry.severity, entry.ts) == ("emitter", "test.ci-broke", "error", NOW - 10)
    assert entry.data["project_id"] == "alpha"


async def test_activity_pages_without_skipping_events_that_share_a_timestamp(env: Env) -> None:
    for index in range(7):
        env.emitter.publish(Noted(project_id="alpha", note=str(index), at=NOW - 20 if index < 5 else NOW - 10))
    await env.settle()

    seen: list[int] = []
    cursor: dict[str, object] = {}
    while True:
        page = await env.events.activity(ActivityInput.model_validate({"project_id": "alpha", "limit": 3, **cursor}))
        seen.extend(entry.id for entry in page.entries)
        if page.next_before is None:
            break
        cursor = {"before": page.next_before, "before_id": page.next_before_id}

    rows = await env.runtime.db.fetch_all("SELECT id FROM events WHERE project_id = 'alpha' ORDER BY ts DESC, id DESC")
    assert seen == [row["id"] for row in rows] and len(seen) == 7


async def test_activity_before_timestamp_alone_pages_strictly_older_events(env: Env) -> None:
    env.emitter.publish(Noted(project_id="alpha", note="old", at=NOW - 30))
    env.emitter.publish(Noted(project_id="alpha", note="new", at=NOW - 10))
    await env.settle()

    assert await env.activity(project_id="alpha", before=NOW - 10) == ["Noted old"]


async def test_a_page_cursor_needs_its_timestamp(env: Env) -> None:
    with pytest.raises(ActionError) as failure:
        await env.runtime.invoke("events", "activity", {"before_id": 4})
    assert failure.value.status == 422


async def test_notifications_count_unread_until_marked_read(env: Env) -> None:
    for _ in range(3):
        env.emitter.publish(CiBroke(project_id="alpha"))
    env.emitter.publish(Noted(project_id="alpha"))
    await env.settle()
    await wait_for(lambda: env.events.state.unread == 3)

    page = await env.events.notifications(NotificationsInput())
    assert page.unread == 3 and len(page.notifications) == 3
    assert {n.kind for n in page.notifications} == {"ci-failure"}
    assert not any(n.read for n in page.notifications)

    first, *rest = (n.id for n in page.notifications)
    assert (await env.events.mark_read(MarkReadInput(ids=[first]))).unread == 2
    assert [n.read for n in env.events.state.notifications if n.id == first] == [True]
    unread_only = await env.events.notifications(NotificationsInput(unread_only=True))
    assert sorted(n.id for n in unread_only.notifications) == sorted(rest)

    assert (await env.events.mark_read(MarkReadInput(mark_all=True))).unread == 0
    assert env.events.state.unread == 0 and all(n.read for n in env.events.state.notifications)


async def test_marking_a_plain_event_read_changes_nothing(env: Env) -> None:
    env.emitter.publish(Noted(project_id="alpha"))
    env.emitter.publish(CiBroke(project_id="alpha"))
    await env.settle()
    plain = await env.runtime.db.fetch_one("SELECT id FROM events WHERE type = 'test.noted'")
    assert plain is not None

    assert (await env.events.mark_read(MarkReadInput(ids=[plain["id"]]))).unread == 1


def test_mark_read_needs_ids_or_all_but_not_both_or_neither() -> None:
    with pytest.raises(ValueError, match="either ids or mark_all"):
        MarkReadInput()
    with pytest.raises(ValueError, match="either ids or mark_all"):
        MarkReadInput(ids=[1], mark_all=True)


async def test_notifications_page_by_id(env: Env) -> None:
    for _ in range(5):
        env.emitter.publish(CiBroke(project_id="alpha"))
    await env.settle()

    first = await env.events.notifications(NotificationsInput(limit=2))
    second = await env.events.notifications(NotificationsInput(limit=2, before_id=first.next_before_id))
    last = await env.events.notifications(NotificationsInput(limit=2, before_id=second.next_before_id))
    ids = [n.id for page in (first, second, last) for n in page.notifications]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 5
    assert last.next_before_id is None


async def test_state_holds_only_the_latest_fifty_notifications(env: Env) -> None:
    for _ in range(55):
        env.emitter.publish(CiBroke(project_id="alpha"))
    await env.settle()
    await wait_for(lambda: env.events.state.unread == 55)

    ids = [n.id for n in env.events.state.notifications]
    assert len(ids) == 50 and ids == sorted(ids, reverse=True)


async def test_notification_center_survives_a_restart(data_paths: DataPaths) -> None:
    async with running(data_paths) as first:
        first.emitter.publish(CiBroke(project_id="alpha"))
        await first.settle()
        await first.events.mark_read(MarkReadInput(mark_all=True))
        first.emitter.publish(CiBroke(project_id="beta"))
        await first.settle()

    async with running(data_paths) as second:
        assert second.events.state.unread == 1
        assert [(n.project_id, n.read, n.kind) for n in second.events.state.notifications] == [
            ("beta", False, "ci-failure"),
            ("alpha", True, "ci-failure"),
        ]


async def test_desktop_notifications_follow_the_per_kind_settings(env: Env) -> None:
    env.emitter.publish(CiBroke(project_id="alpha"))
    env.emitter.publish(ForkLanded(project_id="alpha"))
    await env.settle()
    await wait_for(lambda: len(env.notifier.sent) == 1)
    await asyncio.sleep(0.05)

    [(title, message, urgency)] = env.notifier.sent
    assert (title, message, urgency) == ("alpha: CI failure", "CI failed on main", Urgency.Normal)

    await env.events.update_config(EventsConfig(fork_success=True, ci_failure=False))
    env.emitter.publish(CiBroke(project_id="alpha"))
    env.emitter.publish(ForkLanded(project_id="alpha"))
    await env.settle()
    await wait_for(lambda: len(env.notifier.sent) == 2)
    await asyncio.sleep(0.05)
    assert [(sent[0], sent[2]) for sent in env.notifier.sent[1:]] == [("alpha: Fork synced", Urgency.Low)]


async def test_the_master_switch_silences_every_kind_but_the_center_still_records(env: Env) -> None:
    await env.events.update_config(EventsConfig(desktop_notifications=False))
    env.emitter.publish(CiBroke(project_id="alpha"))
    await env.settle()
    await asyncio.sleep(0.05)

    assert env.notifier.sent == []
    assert env.events.state.unread == 1


async def test_a_kind_without_a_config_toggle_is_recorded_but_never_shown_on_the_desktop(env: Env) -> None:
    env.emitter.publish(Unlisted(project_id="alpha"))
    await env.settle()
    await asyncio.sleep(0.05)

    assert env.notifier.sent == []
    assert [n.kind for n in env.events.state.notifications] == ["no-such-kind"]


async def test_an_event_its_service_mutes_is_neither_notified_nor_listed(env: Env) -> None:
    env.emitter.publish(ForkFailed(project_id="alpha", loud=False))
    env.emitter.publish(ForkFailed(project_id="alpha", loud=True))
    await env.settle()
    await wait_for(lambda: len(env.notifier.sent) == 1)

    assert env.events.state.unread == 1
    assert len(await env.activity(project_id="alpha")) == 2


async def test_a_repeat_observation_notifies_once(env: Env) -> None:
    for _ in range(3):
        env.emitter.publish(CiBroke(project_id="alpha", key="github-run:alpha:7:1"))
    await env.settle()
    await wait_for(lambda: len(env.notifier.sent) == 1)
    await asyncio.sleep(0.05)

    assert len(env.notifier.sent) == 1 and env.events.state.unread == 1


async def test_the_notification_title_names_the_project_by_its_display_name(
    env: Env, make_project: Callable[..., Path]
) -> None:
    await env.discovery.add_project(PathInput(path=str(make_project("Alpha"))))
    await wait_for(lambda: any(record.id == "alpha" for record in env.projects.records()))

    env.emitter.publish(CiBroke(project_id="alpha"))
    env.emitter.publish(CiBroke(project_id="removed-project"))
    env.emitter.publish(CiBroke())
    await env.settle()
    await wait_for(lambda: len(env.notifier.sent) == 3)

    assert sorted(title for title, _message, _urgency in env.notifier.sent) == [
        "Alpha: CI failure",
        "CI failure",
        "removed-project: CI failure",
    ]


async def test_a_failing_notifier_is_logged_and_counted_and_recording_goes_on(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.notifier.failure = RuntimeError("no notification daemon")
    with caplog.at_level(logging.WARNING, logger="forge_dashboard"):
        env.emitter.publish(CiBroke(project_id="alpha"))
        await env.settle()
        await wait_for(lambda: env.events.state.notification_failures == 1)

    assert env.events.state.last_notification_error == "RuntimeError: no notification daemon"
    assert "desktop notification for test.ci-broke failed: no notification daemon" in caplog.text
    env.notifier.failure = None
    env.emitter.publish(CiBroke(project_id="alpha"))
    await env.settle()
    await wait_for(lambda: len(env.notifier.sent) == 1)
    assert env.events.state.unread == 2 and env.events.state.notification_failures == 1


async def test_the_desktop_notifier_is_created_lazily_under_the_app_name(
    data_paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []

    class RecordingNotifier(FakeNotifier):
        def __init__(self, app_name: str) -> None:
            super().__init__()
            created.append(app_name)

    monkeypatch.setattr(events_module, "DesktopNotifier", RecordingNotifier)
    async with running(data_paths, inject_notifier=False) as environment:
        assert created == []
        environment.emitter.publish(CiBroke(project_id="alpha"))
        await wait_for(lambda: created == ["forge dashboard"])


async def test_prune_removes_events_older_than_the_retention_period(env: Env) -> None:
    env.emitter.publish(CiBroke(project_id="alpha", at=NOW - 40 * 86_400))
    env.emitter.publish(Noted(project_id="alpha", note="old", at=NOW - 35 * 86_400))
    env.emitter.publish(Noted(project_id="alpha", note="recent", at=NOW - 10 * 86_400))
    await env.settle()
    assert env.events.state.unread == 1

    await env.events.update_config(EventsConfig(retention_days=30))
    assert await env.events.prune() == 2
    assert await env.activity(project_id="alpha") == ["Noted recent"]
    assert env.events.state.notifications == [] and env.events.state.unread == 0
    assert await env.events.prune() == 0


async def test_events_already_past_retention_are_not_stored_or_notified(env: Env) -> None:
    await env.events.update_config(EventsConfig(retention_days=7))
    env.emitter.publish(CiBroke(project_id="alpha"))
    env.emitter.publish(Noted(project_id="alpha", note="backfilled", at=NOW - 30 * 86_400))
    await env.settle()
    await wait_for(lambda: len(env.notifier.sent) == 1)

    assert await env.activity(project_id="alpha") == ["CI failed on main"]


def test_every_declared_notification_kind_has_a_config_toggle(data_paths: DataPaths) -> None:
    declared = {
        event.notify_kind
        for service in build_runtime(data_paths).services.values()
        for event in service.events
        if event.notify_kind is not None
    }
    assert declared <= set(NOTIFICATION_KINDS)
    assert {kind.replace("-", "_") for kind in NOTIFICATION_KINDS} <= set(EventsConfig.model_fields)
    assert EventsConfig().fork_success is False
    assert all(EventsConfig().notification_enabled(kind) for kind in NOTIFICATION_KINDS if kind != "fork-success")
