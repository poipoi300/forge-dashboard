"""Helpers shared by tests that are plain functions rather than fixtures."""

import asyncio
import time
from collections.abc import Callable


async def wait_for(condition: Callable[[], bool], timeout_s: float = 15.0, interval_s: float = 0.05) -> None:
    """Poll `condition` until it holds; fail the test after `timeout_s`."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout_s}s")
        await asyncio.sleep(interval_s)
