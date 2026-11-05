"""Schedule arithmetic: next run times, jitter, and missed-run catch-up.

Pure functions over aware datetimes, so the rules that decide when a fork
sync or a check runs are tested without clocks or sleeps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Annotated, Literal

from croniter import croniter
from pydantic import AfterValidator, BaseModel, Field

from forge_dashboard.core.ui import ui


def _check_cron(expression: str) -> str:
    if not croniter.is_valid(expression):
        raise ValueError(f"{expression!r} is not a valid cron expression (five fields: m h dom mon dow)")
    return expression


class ManualSchedule(BaseModel):
    """Only when asked."""

    kind: Literal["manual"] = "manual"


class IntervalSchedule(BaseModel):
    """Every fixed interval."""

    kind: Literal["interval"] = "interval"
    every_s: int = Field(3600, ge=60, json_schema_extra=ui(widget="duration", help="Time between runs."))


class CronSchedule(BaseModel):
    """On a cron expression, in local time."""

    kind: Literal["cron"] = "cron"
    expression: Annotated[str, AfterValidator(_check_cron)] = Field(
        "0 6 * * *", json_schema_extra=ui(widget="cron", placeholder="0 6 * * *")
    )


Schedule = Annotated[ManualSchedule | IntervalSchedule | CronSchedule, Field(discriminator="kind")]


def next_run_after(schedule: Schedule, after: datetime, jitter: timedelta = timedelta(0)) -> datetime | None:
    """The first run strictly after `after`, shifted by `jitter`; None for manual schedules.

    Args:
        after: An aware datetime; cron expressions are evaluated in its timezone.
        jitter: An offset the caller drew once for this run (spreads load).
    """
    if isinstance(schedule, ManualSchedule):
        return None
    if isinstance(schedule, IntervalSchedule):
        return after + timedelta(seconds=schedule.every_s) + jitter
    upcoming: datetime = croniter(schedule.expression, after).get_next(datetime)
    return upcoming + jitter


def catch_up(next_run_at: datetime | None, now: datetime) -> datetime | None:
    """A run missed while the dashboard was down happens once, now; never a burst of every missed slot."""
    if next_run_at is None:
        return None
    return max(next_run_at, now)


def draw_jitter(max_jitter_s: float, uniform: Callable[[float, float], float]) -> timedelta:
    """A jitter in [0, max_jitter_s], drawn from an injected source."""
    return timedelta(seconds=uniform(0.0, max_jitter_s)) if max_jitter_s > 0 else timedelta(0)


def due[T](entries: Iterable[tuple[T, datetime | None]], now: datetime, paused: bool) -> list[T]:
    """The entries whose next run has arrived, oldest first; nothing while paused."""
    if paused:
        return []
    ready = [(at, entry) for entry, at in entries if at is not None and at <= now]
    return [entry for _at, entry in sorted(ready, key=lambda pair: pair[0])]
