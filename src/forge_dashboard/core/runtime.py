"""Shared infrastructure every service receives: storage, events, state fan-out, actions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import jsonpatch
from pydantic import BaseModel, JsonValue, ValidationError

from forge_dashboard.core.db import Database
from forge_dashboard.core.events import Event, EventBus, EventEnvelope
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.service import ActionError, BoundAction, Service

STATE_FLUSH_S = 0.05
STATE_QUEUE_LIMIT = 500


class StatePatch(BaseModel):
    """One service's state change on the wire: RFC 6902 operations against revision `rev - 1`."""

    service: str
    rev: int
    ops: list[dict[str, JsonValue]]


class StateSnapshot(BaseModel):
    """A service's full state at a revision."""

    service: str
    rev: int
    state: dict[str, JsonValue]


@dataclass
class _Published:
    rev: int
    document: dict[str, Any]


@dataclass
class StateHub:
    """Coalesces state changes and fans them out as JSON patches."""

    _published: dict[str, _Published] = field(default_factory=dict)
    _dirty: dict[str, Service[Any, Any]] = field(default_factory=dict)
    _subscribers: set[asyncio.Queue[StatePatch | None]] = field(default_factory=set)
    _flush_scheduled: bool = False

    def register(self, service: Service[Any, Any]) -> None:
        self._published[service.id] = _Published(rev=0, document=service.state.model_dump(mode="json"))

    def mark_dirty(self, service: Service[Any, Any]) -> None:
        self._dirty[service.id] = service
        if self._flush_scheduled:
            return
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().call_later(STATE_FLUSH_S, self.flush)
            self._flush_scheduled = True

    def flush(self) -> None:
        self._flush_scheduled = False
        dirty, self._dirty = self._dirty, {}
        for service_id, service in dirty.items():
            published = self._published[service_id]
            document = service.state.model_dump(mode="json")
            ops = jsonpatch.make_patch(published.document, document).patch
            if not ops:
                continue
            published.rev += 1
            published.document = document
            patch = StatePatch(service=service_id, rev=published.rev, ops=ops)
            for queue in tuple(self._subscribers):
                if queue.full():
                    # A stalled client gets a reset marker and resyncs from snapshots.
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(None)
                    continue
                queue.put_nowait(patch)

    def snapshot(self, service_id: str) -> StateSnapshot:
        published = self._published[service_id]
        return StateSnapshot(service=service_id, rev=published.rev, state=published.document)

    def snapshots(self) -> list[StateSnapshot]:
        return [self.snapshot(service_id) for service_id in self._published]

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[StatePatch | None]]:
        """Patches for every service; `None` means "you fell behind, resync from snapshots"."""
        queue: asyncio.Queue[StatePatch | None] = asyncio.Queue(maxsize=STATE_QUEUE_LIMIT)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


class Runtime:
    """What every service shares. Created once per process."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths
        self._db: Database | None = None
        self.bus = EventBus()
        self.states = StateHub()
        self.log = logging.getLogger("forge_dashboard")
        self.services: dict[str, Service[Any, Any]] = {}
        self._actions: dict[tuple[str, str], BoundAction] = {}
        self.dashboard_url: str | None = None

    @property
    def db(self) -> Database:
        if self._db is None:
            raise RuntimeError("the database is opened at startup; it is not available yet")
        return self._db

    async def open(self) -> None:
        """Create the data dir and open the database."""
        self.paths.ensure()
        self._db = await Database.open(self.paths.database)

    async def start(self) -> None:
        """Open storage (unless already open), load every service's config, then start them in registration order."""
        if self._db is None:
            await self.open()
        for service in self.services.values():
            await service.load_config()
        for service in self.services.values():
            await service.start()

    async def stop(self) -> None:
        """Stop services in reverse order, then close storage."""
        for service in reversed(list(self.services.values())):
            try:
                await service.stop()
            except Exception:  # one service failing to stop must not leave the others running
                self.log.exception("service %s failed to stop", service.id)
        if self._db is not None:
            await self._db.close()
            self._db = None

    def register(self, services: Iterable[Service[Any, Any]]) -> None:
        for service in services:
            if service.id in self.services:
                raise ValueError(f"duplicate service id {service.id!r}")
            self.services[service.id] = service
            self.states.register(service)
            for bound in service.bound_actions():
                self._actions[(service.id, bound.id)] = bound

    def state_changed(self, service: Service[Any, Any]) -> None:
        self.states.mark_dirty(service)

    def publish(self, service_id: str, event: Event) -> EventEnvelope:
        return self.bus.publish(service_id, event)

    def action(self, service_id: str, action_id: str) -> BoundAction:
        try:
            return self._actions[(service_id, action_id)]
        except KeyError:
            raise ActionError(f"unknown action {service_id}.{action_id}", status=404) from None

    async def invoke(self, service_id: str, action_id: str, payload: dict[str, JsonValue]) -> BaseModel:
        """Validate `payload` against the action's input model and run it (used by the scheduler and CLI)."""
        bound = self.action(service_id, action_id)
        try:
            request = bound.input_model.model_validate(payload)
        except ValidationError as error:
            raise ActionError(f"invalid input for {service_id}.{action_id}: {error}", status=422) from error
        return await bound.call(request)
