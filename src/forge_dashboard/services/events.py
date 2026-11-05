"""The event log, the notification center, and OS desktop notifications.

Every service publishes typed events on the bus. This service subscribes at
startup and writes each event that asks to be persisted into the `events`
table, which feeds every activity view. An event carrying a notification kind
also appears in the notification center (with an unread count) and, when its
kind is enabled in the config, raises an operating-system notification.

The kind of a stored notification is not a column: it is read back from the
`notify_kind` the event class declares, found through the registered services'
`events`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Protocol, Self, override

from desktop_notifier import DesktopNotifier, Urgency
from pydantic import BaseModel, Field, JsonValue, model_validator

from forge_dashboard.core.events import EventEnvelope, Severity
from forge_dashboard.core.service import Service, action
from forge_dashboard.core.tasks import BackgroundTasks, repeat
from forge_dashboard.core.ui import ui
from forge_dashboard.services.projects import ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

APP_NAME = "forge dashboard"
SECONDS_PER_DAY = 86_400
PRUNE_PERIOD_S = 3600
NOTIFICATION_LIMIT = 50

NOTIFICATION_KINDS: dict[str, str] = {
    "ci-failure": "CI failure",
    "fork-conflict": "Fork needs attention",
    "fork-failure": "Fork sync failed",
    "fork-success": "Fork synced",
    "scheduled-failure": "Scheduled run failed",
    "check-regression": "Check regressed",
    "service-crash": "Service crashed",
}

SEVERITY_UI = ui(
    format="badge",
    enum_labels={"info": "info", "success": "success", "warning": "warning", "error": "error"},
    tones={"info": "info", "success": "good", "warning": "warn", "error": "bad"},
)
_DESKTOP_GROUP = "Desktop notifications"


class Notifier(Protocol):
    """What raising an OS notification needs; `DesktopNotifier` satisfies it."""

    async def send(self, title: str, message: str, urgency: Urgency) -> str: ...


def _kind_toggle(default: bool, help_text: str, order: int) -> Any:
    return Field(
        default,
        json_schema_extra=ui(
            widget="toggle",
            group=_DESKTOP_GROUP,
            order=order,
            help=help_text,
            visible_if={"field": "desktop_notifications", "equals": True},
        ),
    )


class EventsConfig(BaseModel):
    desktop_notifications: bool = Field(
        True,
        json_schema_extra=ui(
            widget="toggle",
            group=_DESKTOP_GROUP,
            order=0,
            help="Master switch for operating-system notifications. The notification center always records.",
        ),
    )
    ci_failure: bool = _kind_toggle(True, "CI failed on a project's default branch.", 1)
    fork_conflict: bool = _kind_toggle(True, "A fork sync needs attention (conflicts, failing checks).", 2)
    fork_failure: bool = _kind_toggle(True, "A fork sync failed.", 3)
    fork_success: bool = _kind_toggle(False, "A fork sync landed cleanly.", 4)
    scheduled_failure: bool = _kind_toggle(True, "A scheduled run failed.", 5)
    check_regression: bool = _kind_toggle(True, "A check went from passing to failing.", 6)
    service_crash: bool = _kind_toggle(True, "A project's service crashed.", 7)
    retention_days: int = Field(
        90,
        ge=1,
        le=3650,
        json_schema_extra=ui(group="Retention", unit="days", help="Events older than this are pruned from the log."),
    )

    def notification_enabled(self, kind: str) -> bool:
        """Whether an event of notification `kind` raises an OS notification; unknown kinds never do."""
        if not self.desktop_notifications or kind not in NOTIFICATION_KINDS:
            return False
        return bool(getattr(self, kind.replace("-", "_")))


class Notification(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    ts: float = Field(json_schema_extra=ui(format="relative-time", short_label="when"))
    kind: str = Field(json_schema_extra=ui(format="badge"))
    title: str
    severity: Severity = Field(json_schema_extra=SEVERITY_UI)
    project_id: str | None = Field(None, json_schema_extra=ui(short_label="project"))
    read: bool


class EventsState(BaseModel):
    unread: int = Field(0, json_schema_extra=ui(short_label="unread"))
    notifications: list[Notification] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["severity", "title", "project_id", "ts"], help="The latest notifications."),
    )
    notification_failures: int = Field(0, json_schema_extra=ui(help="Desktop notifications that could not be sent."))
    last_notification_error: str | None = Field(None, json_schema_extra=ui(format="code"))


class ActivityInput(BaseModel):
    project_id: str | None = Field(None, json_schema_extra=ui(hidden=True, bind="project"))
    services: list[str] = Field(
        default_factory=list, json_schema_extra=ui(widget="tags", help="Only events from these services.")
    )
    types: list[str] = Field(
        default_factory=list, json_schema_extra=ui(widget="tags", help="Only these event types, e.g. git.commit.")
    )
    before: float | None = Field(None, json_schema_extra=ui(hidden=True, help="Page cursor: timestamp."))
    before_id: int | None = Field(None, json_schema_extra=ui(hidden=True, help="Page cursor: id among equal times."))
    limit: int = Field(50, ge=1, le=500)

    @model_validator(mode="after")
    def _cursor_is_complete(self) -> Self:
        if self.before_id is not None and self.before is None:
            raise ValueError("before_id needs before")
        return self


class ActivityEntry(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    ts: float = Field(json_schema_extra=ui(format="relative-time", short_label="when"))
    service: str = Field(json_schema_extra=ui(format="code"))
    type: str = Field(json_schema_extra=ui(format="code"))
    project_id: str | None = Field(None, json_schema_extra=ui(short_label="project"))
    severity: Severity = Field(json_schema_extra=SEVERITY_UI)
    title: str
    data: dict[str, JsonValue] = Field(json_schema_extra=ui(widget="json", advanced=True))


class ActivityPage(BaseModel):
    entries: list[ActivityEntry] = Field(
        json_schema_extra=ui(columns=["ts", "severity", "title", "project_id", "type"])
    )
    next_before: float | None = Field(None, json_schema_extra=ui(hidden=True))
    next_before_id: int | None = Field(None, json_schema_extra=ui(hidden=True))


class NotificationsInput(BaseModel):
    unread_only: bool = False
    before_id: int | None = Field(None, json_schema_extra=ui(hidden=True, help="Page cursor."))
    limit: int = Field(NOTIFICATION_LIMIT, ge=1, le=200)


class NotificationPage(BaseModel):
    notifications: list[Notification] = Field(json_schema_extra=ui(columns=["severity", "title", "project_id", "ts"]))
    unread: int
    next_before_id: int | None = Field(None, json_schema_extra=ui(hidden=True))


class MarkReadInput(BaseModel):
    ids: list[int] = Field(default_factory=list, json_schema_extra=ui(hidden=True, help="Notifications to mark."))
    mark_all: bool = Field(False, json_schema_extra=ui(widget="toggle", help="Mark every notification read."))

    @model_validator(mode="after")
    def _names_what_to_mark(self) -> Self:
        if bool(self.ids) == self.mark_all:
            raise ValueError("give either ids or mark_all")
        return self


class UnreadCount(BaseModel):
    unread: int


class EventsService(Service[EventsConfig, EventsState]):
    id = "events"
    title = "Events"
    description = "The event log behind every activity view, the notification center, and desktop notifications."
    Config = EventsConfig
    State = EventsState

    def __init__(self, runtime: Runtime, projects: ProjectsService, *, notifier: Notifier | None = None) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._notifier = notifier
        self._background = BackgroundTasks()
        self._subscription = AsyncExitStack()
        self._notify_kinds: dict[str, str] = {}

    @override
    async def start(self) -> None:
        # Subscribe before returning: an event published while later services start must not be missed.
        queue = await self._subscription.enter_async_context(self.runtime.bus.subscribe())
        self._notify_kinds = {
            event.type: event.notify_kind
            for service in self.runtime.services.values()
            for event in service.events
            if event.notify_kind is not None
        }
        await self._refresh_state()
        self._background.spawn(self._record_forever(queue), name="events-record")
        self._background.spawn(
            repeat(self._prune_and_log, lambda: PRUNE_PERIOD_S, label="events prune"), "events-prune"
        )

    @override
    async def stop(self) -> None:
        await self._background.cancel_all()
        await self._subscription.aclose()

    # --- recording ---

    async def _record_forever(self, queue: asyncio.Queue[EventEnvelope]) -> None:
        while True:
            envelope = await queue.get()
            try:
                await self._record(envelope)
            except Exception:  # one unrecordable event must not end the recorder
                self.runtime.log.exception("could not record event %s", envelope.type)

    async def _record(self, envelope: EventEnvelope) -> None:
        if envelope.persist and not await self._store(envelope):
            return
        if envelope.notify_kind is None:
            return
        if envelope.persist:
            await self._refresh_state()
        self._raise_desktop_notification(envelope, envelope.notify_kind)

    async def _store(self, envelope: EventEnvelope) -> bool:
        """Write the event to the log; False when it is a repeat observation or already past retention."""
        if envelope.ts < self._retention_cutoff():
            return False
        # Only this recorder writes `events`, so the read cannot race another insert. It tells a new fact (which
        # may notify) from a repeat observation; the unique key stays the authority on what is stored.
        if envelope.dedupe_key is not None:
            seen = await self.runtime.db.fetch_one("SELECT 1 FROM events WHERE dedupe_key = ?", (envelope.dedupe_key,))
            if seen is not None:
                return False
        await self.runtime.db.execute(
            "INSERT OR IGNORE INTO events (ts, service, type, project_id, severity, title, data, notify, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                envelope.ts,
                envelope.service,
                envelope.type,
                envelope.project_id,
                envelope.severity,
                envelope.title,
                json.dumps(envelope.data),
                int(envelope.notify_kind is not None),
                envelope.dedupe_key,
            ),
        )
        return True

    def _retention_cutoff(self) -> float:
        return time.time() - self.config.retention_days * SECONDS_PER_DAY

    async def prune(self) -> int:
        """Delete events older than the retention period; returns how many were removed."""
        cutoff = self._retention_cutoff()
        counted = await self.runtime.db.fetch_all("SELECT COUNT(*) AS n FROM events WHERE ts < ?", (cutoff,))
        removed = int(counted[0]["n"])
        if removed:
            await self.runtime.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            await self._refresh_state()
        return removed

    async def _prune_and_log(self) -> None:
        removed = await self.prune()
        if removed:
            self.runtime.log.info("pruned %d events older than %d days", removed, self.config.retention_days)

    # --- desktop notifications ---

    def _raise_desktop_notification(self, envelope: EventEnvelope, kind: str) -> None:
        if not self.config.notification_enabled(kind):
            return
        self._background.spawn(self._deliver(envelope, kind), name=f"events-notify-{envelope.seq}")

    async def _deliver(self, envelope: EventEnvelope, kind: str) -> None:
        label = NOTIFICATION_KINDS[kind]
        project = self._project_label(envelope.project_id)
        urgency = Urgency.Normal if envelope.severity in ("warning", "error") else Urgency.Low
        try:
            if self._notifier is None:
                self._notifier = DesktopNotifier(app_name=APP_NAME)
            await self._notifier.send(
                title=f"{project}: {label}" if project is not None else label, message=envelope.title, urgency=urgency
            )
        except Exception as error:  # a broken notification backend must never take the service down
            self.runtime.log.warning("desktop notification for %s failed: %s", envelope.type, error)
            self.update_state(
                notification_failures=self.state.notification_failures + 1,
                last_notification_error=f"{type(error).__name__}: {error}",
            )

    def _project_label(self, project_id: str | None) -> str | None:
        if project_id is None:
            return None
        record = next((record for record in self._projects.records() if record.id == project_id), None)
        if record is None:
            return project_id
        return record.manifest.project.name if record.manifest is not None else record.path.name

    # --- state ---

    async def _refresh_state(self) -> None:
        rows = await self.runtime.db.fetch_all(
            "SELECT * FROM events WHERE notify = 1 ORDER BY id DESC LIMIT ?", (NOTIFICATION_LIMIT,)
        )
        self.update_state(unread=await self._unread_count(), notifications=[self._notification(row) for row in rows])

    async def _unread_count(self) -> int:
        counted = await self.runtime.db.fetch_all("SELECT COUNT(*) AS n FROM events WHERE notify = 1 AND read = 0")
        return int(counted[0]["n"])

    def _notification(self, row: sqlite3.Row) -> Notification:
        return Notification(
            id=row["id"],
            ts=row["ts"],
            kind=self._notify_kinds.get(row["type"], row["type"]),
            title=row["title"],
            severity=row["severity"],
            project_id=row["project_id"],
            read=bool(row["read"]),
        )

    # --- actions ---

    @action(title="Activity", query=True)
    async def activity(self, request: ActivityInput) -> ActivityPage:
        """Events newest first, for one project or all, filtered by service or type and paged by timestamp."""
        clauses: list[str] = []
        params: list[object] = []
        if request.project_id is not None:
            clauses.append("project_id = ?")
            params.append(request.project_id)
        for column, values in (("service", request.services), ("type", request.types)):
            if values:
                clauses.append(f"{column} IN ({_placeholders(len(values))})")
                params.extend(values)
        if request.before is not None and request.before_id is not None:
            clauses.append("(ts < ? OR (ts = ? AND id < ?))")
            params.extend((request.before, request.before, request.before_id))
        elif request.before is not None:
            clauses.append("ts < ?")
            params.append(request.before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.runtime.db.fetch_all(
            f"SELECT * FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?", (*params, request.limit + 1)
        )
        page, more = rows[: request.limit], len(rows) > request.limit
        entries = [
            ActivityEntry(
                id=row["id"],
                ts=row["ts"],
                service=row["service"],
                type=row["type"],
                project_id=row["project_id"],
                severity=row["severity"],
                title=row["title"],
                data=json.loads(row["data"]),
            )
            for row in page
        ]
        last = entries[-1] if more else None
        return ActivityPage(
            entries=entries,
            next_before=last.ts if last is not None else None,
            next_before_id=last.id if last is not None else None,
        )

    @action(title="Notifications", query=True)
    async def notifications(self, request: NotificationsInput) -> NotificationPage:
        """Notifications newest first, paged by id."""
        clauses = ["notify = 1"]
        params: list[object] = []
        if request.unread_only:
            clauses.append("read = 0")
        if request.before_id is not None:
            clauses.append("id < ?")
            params.append(request.before_id)
        rows = await self.runtime.db.fetch_all(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
            (*params, request.limit + 1),
        )
        page, more = rows[: request.limit], len(rows) > request.limit
        notifications = [self._notification(row) for row in page]
        return NotificationPage(
            notifications=notifications,
            unread=await self._unread_count(),
            next_before_id=notifications[-1].id if more else None,
        )

    @action(title="Mark notifications read")
    async def mark_read(self, request: MarkReadInput) -> UnreadCount:
        """Mark some notifications (by id) or all of them read."""
        if request.mark_all:
            await self.runtime.db.execute("UPDATE events SET read = 1 WHERE notify = 1 AND read = 0")
        else:
            await self.runtime.db.execute(
                f"UPDATE events SET read = 1 WHERE notify = 1 AND id IN ({_placeholders(len(request.ids))})",
                request.ids,
            )
        await self._refresh_state()
        return UnreadCount(unread=self.state.unread)


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)
