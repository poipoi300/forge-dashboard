"""Typed events and the in-process bus that fans them out.

Every event is a pydantic model with a dotted `type`. Publishing wraps it in
an `EventEnvelope`; subscribers (the WebSocket stream, the event log, the
notifier) each get their own bounded queue, so a slow client can never block
a publisher.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

Severity = Literal["info", "success", "warning", "error"]


class Event(BaseModel):
    """Base class for typed events.

    Class attributes:
        type: Dotted event name, unique across services.
        persist: Whether the event is written to the activity log.
        notify_kind: Notification kind this event can raise, if any.
    """

    model_config = ConfigDict(frozen=True)

    type: ClassVar[str]
    persist: ClassVar[bool] = False
    notify_kind: ClassVar[str | None] = None

    project_id: str | None = None

    def title(self) -> str:
        """One-line human summary for the activity log and notifications."""
        return self.type

    def severity(self) -> Severity:
        return "info"

    def dedupe_key(self) -> str | None:
        """A stable key when the same fact may be observed more than once (a commit, a CI run)."""
        return None

    def occurred_at(self) -> float | None:
        """When the underlying fact happened, if not now (backfilled commits, CI runs)."""
        return None

    def should_notify(self) -> bool:
        """Whether this occurrence raises its `notify_kind` (a fork policy may mute success, say)."""
        return True


class EventEnvelope(BaseModel):
    """An event on the wire."""

    seq: int
    ts: float
    service: str
    type: str
    project_id: str | None
    title: str
    severity: Severity
    data: dict[str, JsonValue]
    persist: bool
    notify_kind: str | None
    dedupe_key: str | None


QUEUE_LIMIT = 1000


class EventBus:
    """In-process publish/subscribe for `EventEnvelope`s."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[EventEnvelope]] = set()
        self._sequence = itertools.count(1)

    def publish(self, service: str, event: Event) -> EventEnvelope:
        envelope = EventEnvelope(
            seq=next(self._sequence),
            ts=event.occurred_at() or time.time(),
            service=service,
            type=event.type,
            project_id=event.project_id,
            title=event.title(),
            severity=event.severity(),
            data=event.model_dump(mode="json"),
            persist=event.persist,
            notify_kind=event.notify_kind if event.should_notify() else None,
            dedupe_key=event.dedupe_key(),
        )
        for queue in tuple(self._subscribers):
            if queue.full():
                # The subscriber stopped draining; drop its oldest item rather than block publishers.
                queue.get_nowait()
            queue.put_nowait(envelope)
        return envelope

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[EventEnvelope]]:
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
