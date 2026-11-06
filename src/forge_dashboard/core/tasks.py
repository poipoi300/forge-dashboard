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


async def repeat(
    action: Callable[[], Awaitable[object]],
    period_s: Callable[[], float],
    *,
    label: str,
    first_run_after_s: float = 0.0,
    wake: asyncio.Event | None = None,
) -> None:
    """Run `action` every `period_s()` seconds forever; a failed run is logged and retried next period.

    Args:
        action: One iteration. Its result is ignored.
        period_s: Seconds to wait after each run, read afresh every time so a changed config applies at once.
        label: Names the loop in the log line of a failed run.
        first_run_after_s: Seconds to wait before the first run.
        wake: When set, ends the current wait early; the event is cleared as the next run starts.
    """
    if first_run_after_s > 0:
        await asyncio.sleep(first_run_after_s)
    while True:
        try:
            await action()
        except Exception:  # one failed iteration must not end a loop that runs for the life of the process
            _log.exception("%s failed", label)
        if wake is None:
            await asyncio.sleep(period_s())
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=period_s())
        wake.clear()
