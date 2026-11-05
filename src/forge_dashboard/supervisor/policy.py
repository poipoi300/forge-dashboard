"""Restart and retry decisions as pure functions over failure timestamps.

Kept free of I/O and clocks so the rules that decide whether a crashing
service is retried, how long to wait, and when to give up are unit-tested
directly. Callers pass `now` from one injected monotonic clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

INITIAL_BACKOFF_S = 1.0
BACKOFF_FACTOR = 2.0
MAX_BACKOFF_S = 30.0

# Five crashes inside a minute means the cause is not transient (a broken
# build, a taken port); retrying further only hides the reason.
MAX_CRASHES = 5
CRASH_WINDOW_S = 60.0

# Pre-step (install/build) failures have their own, smaller budget so a flaky
# `bun install` never spends the runtime-crash budget, nor the reverse.
MAX_BUILD_FAILURES = 3
BUILD_FAILURE_WINDOW_S = 120.0

# A child that stays up this long started successfully; its crash history is cleared.
HEALTHY_UPTIME_S = 30.0


class ServiceState(StrEnum):
    """Lifecycle of one supervised service."""

    STOPPED = "stopped"
    BUILDING = "building"
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    STOPPING = "stopping"
    FAILED = "failed"
    BUILD_FAILED = "build failed"
    BLOCKED = "blocked"


# States in which the supervisor is still trying to keep a process up.
ACTIVE_STATES = frozenset({ServiceState.BUILDING, ServiceState.STARTING, ServiceState.RUNNING, ServiceState.BACKOFF})


def next_delay(previous_delay_s: float | None) -> float:
    """Exponential backoff from 1s, doubling, capped at 30s."""
    if previous_delay_s is None:
        return INITIAL_BACKOFF_S
    return min(previous_delay_s * BACKOFF_FACTOR, MAX_BACKOFF_S)


def _recent(moments: Iterable[float], now: float, window_s: float) -> int:
    return sum(1 for moment in moments if now - moment <= window_s)


def should_restart(crashes: Iterable[float], now: float, autorestart: bool) -> bool:
    """Whether a crashed service is restarted, given the times of its recent crashes (this one included)."""
    return autorestart and _recent(crashes, now, CRASH_WINDOW_S) < MAX_CRASHES


def should_retry_build(build_failures: Iterable[float], now: float, autorestart: bool) -> bool:
    """Whether a failed pre-step is retried, against its own separate budget."""
    return autorestart and _recent(build_failures, now, BUILD_FAILURE_WINDOW_S) < MAX_BUILD_FAILURES


def run_was_healthy(uptime_s: float) -> bool:
    """Whether a run lasted long enough to clear the crash history."""
    return uptime_s >= HEALTHY_UPTIME_S
