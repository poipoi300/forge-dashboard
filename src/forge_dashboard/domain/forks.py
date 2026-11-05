"""Fork sync rules: the conflict brief, resolution verification, and run bookkeeping."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field

from forge_dashboard.core.ui import ui
from forge_dashboard.domain.gitparse import Commit
from forge_dashboard.domain.scheduling import ManualSchedule, Schedule

CONFLICT_MARKER = re.compile(r"^(<<<<<<<|=======|>>>>>>>)( |$)", re.MULTILINE)
MAX_SIDE_CHARS = 12_000
MAX_COMMITS_LISTED = 40

NotifyOn = Literal["success", "conflict", "failure"]


def _default_notify() -> list[NotifyOn]:
    return ["conflict", "failure"]


class ForkPolicy(BaseModel):
    """How and when a fork follows its upstream."""

    upstream_url: str | None = Field(
        None,
        json_schema_extra=ui(
            group="Upstream",
            order=1,
            placeholder="https://github.com/owner/repo.git",
            help="Setting this adds or updates the `upstream` remote.",
        ),
    )
    upstream_branch: str | None = Field(
        None, json_schema_extra=ui(group="Upstream", order=2, placeholder="detected default branch")
    )
    target_branch: str | None = Field(
        None, json_schema_extra=ui(group="Target", order=1, placeholder="the fork's default branch")
    )
    strategy: Literal["merge", "rebase"] = Field(
        "merge",
        json_schema_extra=ui(
            widget="radio-cards",
            group="Target",
            order=2,
            enum_labels={"merge": "Merge", "rebase": "Rebase"},
            enum_descriptions={
                "merge": "Merge upstream into the target; history is kept and the merge commit records the sync.",
                "rebase": "Replay the fork's commits on top of upstream; the target branch is rewritten.",
            },
        ),
    )
    schedule: Annotated[Schedule, Field(discriminator="kind")] = Field(
        default_factory=ManualSchedule, json_schema_extra=ui(group="Schedule", order=1)
    )
    auto_push: bool = Field(
        False, json_schema_extra=ui(widget="toggle", group="After a sync", help="Push the target to origin.")
    )
    conflicts: Literal["agent", "stop"] = Field(
        "agent",
        json_schema_extra=ui(
            widget="radio-cards",
            group="Conflicts",
            enum_labels={"agent": "Resolve with an agent", "stop": "Stop and notify"},
            enum_descriptions={
                "agent": "The harness edits the conflicted files; the dashboard verifies and commits.",
                "stop": "Park the run with the conflict for you to resolve.",
            },
        ),
    )
    max_agent_attempts: int = Field(
        2,
        ge=1,
        le=5,
        json_schema_extra=ui(
            widget="slider",
            min=1,
            max=5,
            step=1,
            group="Conflicts",
            visible_if={"field": "conflicts", "equals": "agent"},
        ),
    )
    checks: list[str] | None = Field(
        None,
        json_schema_extra=ui(
            widget="tags",
            group="After a sync",
            help="Check ids to run; empty uses the manifest's commit and push checks.",
        ),
    )
    notify: list[NotifyOn] = Field(
        default_factory=_default_notify,
        json_schema_extra=ui(widget="multiselect", group="Notifications"),
    )


class ForkRunState(StrEnum):
    RUNNING = "running"
    SYNCED = "synced"
    UP_TO_DATE = "up-to-date"
    CONFLICT = "conflict"
    NEEDS_ATTENTION = "needs-attention"
    READY_TO_APPLY = "ready-to-apply"
    FAILED = "failed"
    ABORTED = "aborted"


PARKED = frozenset({ForkRunState.CONFLICT, ForkRunState.NEEDS_ATTENTION, ForkRunState.READY_TO_APPLY})

FORK_STATE_UI = ui(
    format="badge",
    tones={
        "running": "info",
        "synced": "good",
        "up-to-date": "good",
        "conflict": "bad",
        "needs-attention": "warn",
        "ready-to-apply": "info",
        "failed": "bad",
        "aborted": "neutral",
    },
)


class ForkRun(BaseModel):
    id: str = Field(json_schema_extra=ui(hidden=True))
    project_id: str
    job_id: str = Field(json_schema_extra=ui(hidden=True))
    state: ForkRunState = Field(json_schema_extra=FORK_STATE_UI)
    strategy: Literal["merge", "rebase"]
    upstream_ref: str = Field(json_schema_extra=ui(format="code"))
    upstream_sha: str = Field(json_schema_extra=ui(format="code"))
    target_branch: str = Field(json_schema_extra=ui(format="code"))
    target_sha: str | None = Field(None, json_schema_extra=ui(format="code", hidden=True))
    branch: str | None = Field(None, json_schema_extra=ui(format="code"))
    worktree: str | None = Field(None, json_schema_extra=ui(format="path"))
    started_at: float = Field(json_schema_extra=ui(format="relative-time"))
    ended_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    duration_s: float | None = Field(None, json_schema_extra=ui(format="duration"))
    conflicted_files: list[str] = Field(default_factory=list)
    agent_attempts: int = 0
    agent_run_ids: list[str] = Field(default_factory=list, json_schema_extra=ui(hidden=True))
    agent_cost_usd: float | None = Field(None, json_schema_extra=ui(unit="USD", short_label="cost"))
    agent_turns: int | None = Field(None, json_schema_extra=ui(short_label="turns"))
    checks: list[tuple[str, str]] = Field(default_factory=list, json_schema_extra=ui(hidden=True))
    result_sha: str | None = Field(None, json_schema_extra=ui(format="code"))
    pushed: bool = False
    detail: str | None = None


def is_conflict_marker_free(text: str) -> bool:
    return CONFLICT_MARKER.search(text) is None


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SIDE_CHARS else text[:MAX_SIDE_CHARS] + "\n[... truncated ...]\n"


@dataclass(frozen=True)
class ConflictedFile:
    path: str
    merged: str
    ours: str | None
    theirs: str | None


def _commit_lines(commits: Sequence[Commit]) -> str:
    shown = [f"- {c.short_sha} {c.subject} ({c.author})" for c in commits[:MAX_COMMITS_LISTED]]
    if len(commits) > MAX_COMMITS_LISTED:
        shown.append(f"- ... and {len(commits) - MAX_COMMITS_LISTED} more")
    return "\n".join(shown) or "- none"


def conflict_brief(
    *,
    strategy: str,
    upstream_ref: str,
    upstream_sha: str,
    target_branch: str,
    files: Iterable[ConflictedFile],
    upstream_commits: Sequence[Commit],
    fork_commits: Sequence[Commit],
    agents_md: str | None,
    previous_failure: str | None,
    extra_instructions: str | None,
) -> str:
    """The prompt for resolving one round of conflicts."""
    motion = "merged into" if strategy == "merge" else "replayed under"
    sections = [
        "# Conflict brief",
        (
            f"You are resolving {strategy} conflicts in a fork. The current directory is an isolated git worktree in "
            f"which `{upstream_ref}` ({upstream_sha[:12]}) is being {motion} the fork's `{target_branch}`."
        ),
        (
            "Rules:\n- Edit the conflicted files only, so that each keeps the intent of both sides.\n"
            "- Remove every conflict marker (<<<<<<<, =======, >>>>>>>).\n"
            "- Do not run git or any other command; the dashboard stages, verifies, and commits.\n"
            "- Keep files syntactically valid (TOML, JSON, YAML, code)."
        ),
    ]
    for item in files:
        parts = [f"### File: {item.path}", f"Current content with conflict markers:\n```\n{_clip(item.merged)}\n```"]
        if item.ours is not None:
            parts.append(f"The fork's side (ours):\n```\n{_clip(item.ours)}\n```")
        if item.theirs is not None:
            parts.append(f"Upstream's side (theirs):\n```\n{_clip(item.theirs)}\n```")
        sections.append("\n\n".join(parts))
    sections.append(f"## Upstream commits since the merge base\n{_commit_lines(upstream_commits)}")
    sections.append(f"## Fork commits since the merge base\n{_commit_lines(fork_commits)}")
    if agents_md:
        sections.append(f"## The project's AGENTS.md\n{_clip(agents_md)}")
    if extra_instructions:
        sections.append(f"## Instructions from the maintainer\n{extra_instructions}")
    if previous_failure:
        sections.append(f"## Previous attempt failed verification\nFix these problems:\n{previous_failure}")
    return "\n\n".join(sections) + "\n"


def parse_problems(path: Path, text: str) -> str | None:
    """A syntax error for TOML, JSON, and YAML files (the hygiene checks); None when it parses or is another kind."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".toml":
            tomllib.loads(text)
        elif suffix == ".json":
            json.loads(text)
        elif suffix in (".yaml", ".yml"):
            list(yaml.safe_load_all(text))
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, yaml.YAMLError) as error:
        return f"{path.name}: {error}"
    return None


def sync_commit_message(
    *, upstream_ref: str, upstream_url: str | None, upstream_sha: str, agent_attempts: int, agent_run_ids: Sequence[str]
) -> str:
    """The merge commit message: records the upstream SHA and whether an agent resolved conflicts."""
    resolved = (
        f"Conflicts resolved by an agent ({agent_attempts} attempt{'s' if agent_attempts != 1 else ''}; "
        f"runs {', '.join(agent_run_ids)})."
        if agent_attempts
        else "Merged without conflicts."
    )
    source = f" from {upstream_url}" if upstream_url else ""
    return (
        f"Sync {upstream_ref} ({upstream_sha[:12]})\n\n"
        f"Merged {upstream_ref}{source} at {upstream_sha}. {resolved}\n\n"
        f"Forge-Sync-Upstream: {upstream_sha}\nForge-Sync-Agent: {'yes' if agent_attempts else 'no'}\n"
    )


def sync_note(*, upstream_sha: str, agent_attempts: int) -> str:
    """The git note a rebased sync leaves on its tip, since a rebase has no merge commit to carry it."""
    return f"Forge-Sync-Upstream: {upstream_sha}\nForge-Sync-Agent: {'yes' if agent_attempts else 'no'}"
