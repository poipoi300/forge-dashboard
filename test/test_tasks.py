"""Periodic background loops: one failed iteration is logged and the loop carries on."""

import asyncio
import contextlib
import logging

import pytest
from testing_support import wait_for

from forge_dashboard.core.tasks import repeat


async def test_a_failed_iteration_is_logged_and_the_loop_runs_again(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("first iteration breaks")

    with caplog.at_level(logging.ERROR, logger="forge_dashboard.tasks"):
        loop = asyncio.create_task(repeat(flaky, lambda: 0.01, label="flaky loop"))
        try:
            await wait_for(lambda: len(calls) >= 3)
        finally:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop
    assert "flaky loop failed" in caplog.text and "first iteration breaks" in caplog.text


async def test_the_first_run_waits_for_the_requested_delay() -> None:
    ran = asyncio.Event()

    async def action() -> None:
        ran.set()

    loop = asyncio.create_task(repeat(action, lambda: 60, label="delayed", first_run_after_s=0.3))
    try:
        await asyncio.sleep(0.1)
        assert not ran.is_set()
        await asyncio.wait_for(ran.wait(), 5)
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop


async def test_a_wake_event_ends_the_wait_early_and_is_cleared() -> None:
    runs: list[int] = []
    wake = asyncio.Event()

    async def action() -> None:
        runs.append(len(runs))

    loop = asyncio.create_task(repeat(action, lambda: 3600, label="wakeable", wake=wake))
    try:
        await wait_for(lambda: len(runs) == 1)
        wake.set()
        await wait_for(lambda: len(runs) == 2)
        assert not wake.is_set()
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
