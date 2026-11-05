"""Fire-and-forget tasks that are still owned: referenced until done, failures logged."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

_log = logging.getLogger("forge_dashboard.tasks")


class BackgroundTasks:
    """Holds references to spawned tasks so they are neither garbage-collected nor silently lost."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coroutine: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _log.error("background task %s failed", task.get_name(), exc_info=task.exception())

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def repeat(action: Callable[[], Awaitable[None]], period_s: Callable[[], float], *, label: str) -> None:
    """Run `action` every `period_s()` seconds forever; a failed run is logged and retried next period."""
    while True:
        try:
            await action()
        except Exception:  # one failed sample must not end a periodic loop
            _log.exception("%s failed", label)
        await asyncio.sleep(period_s())
