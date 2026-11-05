"""Render the overview as a Markdown report."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from forge_dashboard.domain.summary import ProjectSummary


def _ago(timestamp: float | None, now: float) -> str:
    if timestamp is None:
        return "never"
    seconds = max(0, int(now - timestamp))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _checks(summary: ProjectSummary) -> str:
    checks = summary.checks
    if checks is None or not checks.total:
        return "none run"
    state = f"{checks.passed}/{checks.total} passing"
    return f"{state}, stale" if checks.stale else state


def _v1(summary: ProjectSummary) -> str:
    if summary.v1 is None:
        return "no manifest"
    return f"{summary.v1.percent:.0f}% ({summary.v1.caption})"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(projects: Sequence[ProjectSummary], now: float | None = None) -> str:
    """One table row per project, then the items needing attention."""
    moment = now if now is not None else datetime.now(tz=UTC).timestamp()
    stamp = datetime.fromtimestamp(moment, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# forge dashboard report",
        "",
        f"Generated {stamp}. {len(projects)} projects.",
        "",
        "| Project | Status | Health | Last change | V1 | Checks | CI | Issues | Path |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for summary in projects:
        ci = summary.ci.conclusion if summary.ci is not None else "n/a"
        issues = summary.issues
        issue_text = f"{issues.open_issues} open" if issues is not None and issues.open_issues is not None else "n/a"
        row = [
            summary.name,
            summary.status.value,
            summary.health.level.value,
            f"{_ago(summary.last_change.at, moment)} ({summary.last_change.source})",
            _v1(summary),
            _checks(summary),
            ci,
            issue_text,
            f"`{summary.path}`",
        ]
        lines.append("| " + " | ".join(_cell(cell) for cell in row) + " |")
    attention = [(summary.name, item.message) for summary in projects for item in summary.attention]
    lines += ["", "## Needs attention", ""]
    lines += [f"- **{name}**: {message}" for name, message in attention] or ["Nothing needs attention."]
    return "\n".join(lines) + "\n"
