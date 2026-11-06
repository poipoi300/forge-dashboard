"""`ProjectSummary`: one project at a glance, composed from per-source facts.

Each dashboard service owns one fact model (git state, check results, CI,
issues, services, ...) and publishes it per project. `compose_summary` turns
the facts plus the user's own state (status marker, favorite) into the
summary the overview renders, applying the health rollup and V1 progress
math. The UI builds cards and table columns from this model's schema and
`x-ui` hints, so a field added here appears on the overview with no
frontend edit.

Object-valued fields render through two composite conventions documented in
`docs/ARCHITECTURE.md`: `format: "progress"` reads `done`/`total`/`caption`,
and `format: "badge"` reads `level` and lists the remaining fields on hover.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_dashboard.core.ui import ui, with_hints
from forge_dashboard.manifest.models import MetricDisplay

# --- shared vocabularies ---


class Lifecycle(StrEnum):
    IDEA = "idea"
    PLANNING = "planning"
    ACTIVE = "active"
    BLOCKED = "blocked"
    PAUSED = "paused"
    MAINTENANCE = "maintenance"
    SHIPPED = "shipped"
    ARCHIVED = "archived"


# A marker the user sets, not a measured state: a neutral chip, toned only when it asks for help.
LIFECYCLE_UI = ui(
    format="chip",
    enum_labels={status.value: status.value for status in Lifecycle},
    tones={
        "idea": "neutral",
        "planning": "info",
        "active": "good",
        "blocked": "bad",
        "paused": "warn",
        "maintenance": "info",
        "shipped": "good",
        "archived": "neutral",
    },
    short_label="status",
)


class HealthLevel(StrEnum):
    GOOD = "good"
    WARN = "warn"
    FAILING = "failing"
    UNKNOWN = "unknown"


HEALTH_TONES = ui(
    format="badge",
    enum_labels={"good": "healthy", "warn": "warn", "failing": "failing", "unknown": "unknown"},
    tones={"good": "good", "warn": "warn", "failing": "bad", "unknown": "neutral"},
)

ProjectRole = Literal["managed", "unmanifested", "standards-source"]

ROLE_UI = ui(
    format="badge",
    enum_labels={"managed": "managed", "unmanifested": "no manifest", "standards-source": "standards source"},
    tones={"managed": "neutral", "unmanifested": "warn", "standards-source": "info"},
    short_label="role",
)

# --- facts, one model per owning service ---


class ManifestFacts(BaseModel):
    valid: bool
    error_count: int = 0
    first_error: str | None = None


class GitFacts(BaseModel):
    branch: str | None = Field(None, json_schema_extra=ui(format="code", short_label=""))
    head_sha: str | None = Field(None, json_schema_extra=ui(hidden=True))
    dirty: int = Field(
        0, json_schema_extra=ui(short_label="dirty", hide_zero=True, help="Changed and untracked files.")
    )
    ahead: int | None = Field(
        None, json_schema_extra=ui(short_label="ahead", hide_zero=True, help="Commits ahead of the upstream.")
    )
    behind: int | None = Field(
        None, json_schema_extra=ui(short_label="behind", hide_zero=True, help="Commits behind the upstream.")
    )
    unpushed: int = Field(0, json_schema_extra=ui(short_label="unpushed", hide_zero=True))
    stashes: int = Field(0, json_schema_extra=ui(short_label="stashes", hide_zero=True))
    last_commit_at: float | None = Field(None, json_schema_extra=ui(hidden=True))
    last_worktree_change_at: float | None = Field(None, json_schema_extra=ui(hidden=True))
    origin_url: str | None = Field(None, json_schema_extra=ui(hidden=True))
    github_repo: str | None = Field(None, json_schema_extra=ui(hidden=True, help="owner/name when origin is GitHub."))
    has_upstream_remote: bool = Field(False, json_schema_extra=ui(hidden=True))


class CheckFacts(BaseModel):
    result: Literal["never-run", "running", "passing", "failing"] = Field(
        "never-run",
        json_schema_extra=ui(
            format="badge",
            enum_labels={"never-run": "not run yet", "running": "running", "passing": "passing", "failing": "failing"},
            tones={"never-run": "neutral", "running": "info", "passing": "good", "failing": "bad"},
            short_label="",
        ),
    )
    passed: int = Field(0, json_schema_extra=ui(short_label="pass"))
    failed: int = Field(0, json_schema_extra=ui(short_label="fail"))
    total: int = Field(0, json_schema_extra=ui(hidden=True))
    tests_passed: int | None = Field(None, json_schema_extra=ui(short_label="tests"))
    tests_failed: int | None = Field(None, json_schema_extra=ui(short_label="failing tests"))
    ran_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="ran"))
    duration_s: float | None = Field(None, json_schema_extra=ui(format="duration", short_label="took"))
    sha: str | None = Field(None, json_schema_extra=ui(format="code", short_label="at"))
    stale: bool = Field(
        False, json_schema_extra=ui(short_label="stale", help="HEAD moved or the tree changed since the last run.")
    )
    coverage_percent: float | None = Field(None, json_schema_extra=ui(format="percent", short_label="coverage"))


class CiFacts(BaseModel):
    conclusion: Literal["success", "failure", "cancelled", "in_progress", "none"] = Field(
        "none",
        json_schema_extra=ui(
            format="badge",
            enum_labels={
                "success": "passing",
                "failure": "failing",
                "cancelled": "cancelled",
                "in_progress": "running",
                "none": "no runs",
            },
            tones={
                "success": "good",
                "failure": "bad",
                "cancelled": "neutral",
                "in_progress": "info",
                "none": "neutral",
            },
            short_label="ci",
        ),
    )
    workflows: int = Field(0, json_schema_extra=ui(short_label="workflows"))
    failing_workflows: list[str] = Field(default_factory=list, json_schema_extra=ui(short_label="failing"))
    last_run_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="last run"))
    last_run_url: str | None = Field(None, json_schema_extra=ui(format="url", hidden=True))
    open_pr_checks: Literal["passing", "failing", "pending", "none"] = Field(
        "none",
        json_schema_extra=ui(
            format="badge",
            tones={"passing": "good", "failing": "bad", "pending": "info", "none": "neutral"},
            short_label="pr checks",
        ),
    )


GitHubLink = Literal["ok", "not-on-github", "unauthenticated", "rate-limited", "error"]


class IssueFacts(BaseModel):
    github: GitHubLink = Field(
        "not-on-github",
        json_schema_extra=ui(
            format="badge",
            enum_labels={
                "ok": "github",
                "not-on-github": "not on github",
                "unauthenticated": "github: sign in",
                "rate-limited": "github: rate limited",
                "error": "github: error",
            },
            tones={
                "ok": "neutral",
                "not-on-github": "neutral",
                "unauthenticated": "warn",
                "rate-limited": "warn",
                "error": "bad",
            },
            hidden=True,
        ),
    )
    repo: str | None = Field(None, json_schema_extra=ui(hidden=True))
    open_issues: int | None = Field(None, json_schema_extra=ui(short_label="issues"))
    open_prs: int | None = Field(None, json_schema_extra=ui(short_label="PRs"))
    top_labels: list[str] = Field(default_factory=list, json_schema_extra=ui(short_label="labels"))
    newest_issue: str | None = Field(None, json_schema_extra=ui(short_label="newest"))
    html_url: str | None = Field(None, json_schema_extra=ui(format="url", hidden=True))
    milestone_open: int | None = Field(None, json_schema_extra=ui(hidden=True))
    milestone_closed: int | None = Field(None, json_schema_extra=ui(hidden=True))


ServiceStateName = Literal[
    "stopped", "building", "starting", "running", "backoff", "stopping", "failed", "build failed", "blocked", "external"
]
SERVICE_STATE_UI = ui(
    format="badge",
    enum_labels={"external": "running (external)"},
    tones={
        "stopped": "neutral",
        "building": "info",
        "starting": "info",
        "running": "good",
        "backoff": "warn",
        "stopping": "neutral",
        "failed": "bad",
        "build failed": "bad",
        "blocked": "bad",
        "external": "good",
    },
)


class ServiceBrief(BaseModel):
    id: str = Field(json_schema_extra=ui(hidden=True))
    label: str
    state: ServiceStateName = Field(json_schema_extra=SERVICE_STATE_UI)
    address: str | None = Field(None, json_schema_extra=ui(format="code", short_label="address"))
    url: str | None = Field(None, json_schema_extra=ui(format="url", short_label="open"))
    machine: str


class DeploymentBrief(BaseModel):
    id: str = Field(json_schema_extra=ui(hidden=True))
    label: str
    url: str = Field(json_schema_extra=ui(format="url"))
    machine: str | None = None
    environment: str
    health: Literal["up", "down", "unchecked"] = Field(
        "unchecked", json_schema_extra=ui(format="badge", tones={"up": "good", "down": "bad", "unchecked": "neutral"})
    )


class ServicesFacts(BaseModel):
    services: list[ServiceBrief] = Field(default_factory=list)
    deployments: list[DeploymentBrief] = Field(default_factory=list)


class ForkFacts(BaseModel):
    is_fork: bool = Field(False, json_schema_extra=ui(hidden=True))
    upstream: str | None = Field(None, json_schema_extra=ui(short_label="upstream"))
    behind_upstream: int | None = Field(None, json_schema_extra=ui(short_label="behind upstream", hide_zero=True))
    ahead_of_upstream: int | None = Field(None, json_schema_extra=ui(hidden=True))
    last_sync: Literal["synced", "conflict", "needs-attention", "ready-to-apply", "failed", "never"] = Field(
        "never",
        json_schema_extra=ui(
            format="badge",
            tones={
                "synced": "good",
                "conflict": "bad",
                "needs-attention": "warn",
                "ready-to-apply": "info",
                "failed": "bad",
                "never": "neutral",
            },
            short_label="last sync",
        ),
    )
    last_sync_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="synced"))
    next_sync_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="next sync"))


class StandardsFacts(BaseModel):
    passing: int = Field(0, json_schema_extra=ui(short_label="passing"))
    total: int = Field(0, json_schema_extra=ui(short_label="of"))
    failing: list[str] = Field(default_factory=list, json_schema_extra=ui(hidden=True))
    drift: bool = Field(
        False, json_schema_extra=ui(short_label="behind forge", help="Behind forge's HEAD or failing checks.")
    )
    forge_commits_behind: int | None = Field(
        None, json_schema_extra=ui(short_label="forge commits behind", hide_zero=True)
    )


class PinnedMetric(BaseModel):
    id: str = Field(json_schema_extra=ui(hidden=True))
    label: str
    display: MetricDisplay
    unit: str | None = None
    value: bool | int | float | str | None = None
    status: Literal["ok", "warn", "fail"] | None = Field(
        None, json_schema_extra=ui(format="badge", tones={"ok": "good", "warn": "warn", "fail": "bad"})
    )
    history: list[float] = Field(default_factory=list, json_schema_extra=ui(hidden=True))
    error: str | None = None


class MetricsFacts(BaseModel):
    pinned: list[PinnedMetric] = Field(default_factory=list)


class AgentFacts(BaseModel):
    active: bool = Field(
        False,
        json_schema_extra=ui(
            short_label="agent working",
            tones={"true": "info"},
            help="A claude process is running inside the project now.",
        ),
    )
    last_session_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="last agent"))
    session_count: int = Field(0, json_schema_extra=ui(short_label="sessions"))


class V1Facts(BaseModel):
    """V1 inputs that are not in the manifest: GitHub milestone counts."""

    github_open: int | None = None
    github_closed: int | None = None


# --- the composed summary ---


class HealthReason(BaseModel):
    source: Literal["ci", "checks", "manifest", "standards", "services"]
    level: HealthLevel
    message: str


class Health(BaseModel):
    """Computed rollup; `reasons` explain it."""

    model_config = ConfigDict(json_schema_extra=ui(format="badge"))

    level: HealthLevel = Field(HealthLevel.UNKNOWN, json_schema_extra=HEALTH_TONES)
    reasons: list[HealthReason] = Field(default_factory=list)


class V1Progress(BaseModel):
    """V1 progress bar: weighted done over total, with the next milestone and target date."""

    model_config = ConfigDict(json_schema_extra=ui(format="progress"))

    done: float = 0
    total: float = 0
    percent: float = Field(0, json_schema_extra=ui(format="percent"))
    milestones_done: int = 0
    milestones_total: int = 0
    next_milestone: str | None = Field(None, json_schema_extra=ui(short_label="next"))
    target_date: date | None = Field(None, json_schema_extra=ui(short_label="target"))
    days_left: int | None = Field(None, json_schema_extra=ui(unit="days", short_label="left"))
    caption: str = ""
    detail: str = Field("", json_schema_extra=ui(help="The line under the bar: what comes next."))


class LastChange(BaseModel):
    """The newer of the last commit and the last working-tree edit, and which one it was."""

    at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label=""))
    source: Literal["commit", "working-tree", "unknown"] = Field(
        "unknown",
        json_schema_extra=ui(
            short_label="",
            enum_labels={"commit": "commit", "working-tree": "uncommitted edits", "unknown": "no history"},
        ),
    )


class AttentionItem(BaseModel):
    source: str
    level: Literal["warn", "bad"]
    message: str


class ProjectSummary(BaseModel):
    """One project at a glance."""

    id: str = Field(json_schema_extra=ui(format="code", hidden=True))
    name: str = Field(json_schema_extra=ui(slot="title", order=1))
    summary: str | None = Field(None, json_schema_extra=ui(slot="subtitle"))
    role: ProjectRole = Field(json_schema_extra=with_hints(ROLE_UI, slot="meta"))
    kind: str | None = Field(None, json_schema_extra=ui(slot="meta", format="badge", short_label="kind"))
    languages: list[str] = Field(default_factory=list, json_schema_extra=ui(slot="meta", short_label="languages"))
    tags: list[str] = Field(default_factory=list, json_schema_extra=ui(slot="meta", hidden=True))
    path: str = Field(json_schema_extra=ui(format="path", slot="footer"))
    machine: str = Field(json_schema_extra=ui(slot="footer", short_label="machine"))
    favorite: bool = Field(False, json_schema_extra=ui(hidden=True))
    status: Lifecycle = Field(Lifecycle.ACTIVE, json_schema_extra=with_hints(LIFECYCLE_UI, slot="status", order=3))
    status_note: str | None = Field(None, json_schema_extra=ui(hidden=True))
    # Health leads the status slot: it is measured, so it outranks the marker the user set.
    health: Health = Field(default_factory=Health, json_schema_extra=ui(slot="status", short_label="health", order=2))
    last_change: LastChange = Field(
        default_factory=LastChange, json_schema_extra=ui(slot="status", short_label="changed", order=4)
    )
    v1: V1Progress | None = Field(None, json_schema_extra=ui(slot="progress", short_label="V1"))
    checks: CheckFacts | None = Field(
        None,
        json_schema_extra=ui(
            slot="signals",
            short_label="checks",
            columns=["result", "tests_passed", "coverage_percent", "stale", "ran_at"],
        ),
    )
    ci: CiFacts | None = Field(
        None,
        json_schema_extra=ui(slot="signals", short_label="CI", columns=["conclusion", "open_pr_checks", "last_run_at"]),
    )
    issues: IssueFacts | None = Field(
        None,
        json_schema_extra=ui(slot="signals", short_label="issues", columns=["open_issues", "open_prs", "newest_issue"]),
    )
    git: GitFacts | None = Field(
        None,
        json_schema_extra=ui(
            slot="signals", short_label="git", columns=["branch", "dirty", "ahead", "behind", "unpushed", "stashes"]
        ),
    )
    fork: ForkFacts | None = Field(
        None,
        json_schema_extra=ui(
            slot="signals", short_label="fork", columns=["behind_upstream", "last_sync", "next_sync_at"]
        ),
    )
    standards: StandardsFacts | None = Field(
        None,
        json_schema_extra=ui(
            slot="signals", short_label="standards", columns=["passing", "total", "drift", "forge_commits_behind"]
        ),
    )
    services: list[ServiceBrief] = Field(
        default_factory=list, json_schema_extra=ui(slot="body", columns=["label", "state", "address"])
    )
    deployments: list[DeploymentBrief] = Field(
        default_factory=list, json_schema_extra=ui(slot="body", columns=["label", "health", "machine"])
    )
    metrics: list[PinnedMetric] = Field(
        default_factory=list, json_schema_extra=ui(slot="body", columns=["label", "value", "status"])
    )
    agent: AgentFacts = Field(default_factory=AgentFacts, json_schema_extra=ui(slot="footer", short_label="agent"))
    manifest: ManifestFacts | None = Field(None, json_schema_extra=ui(hidden=True))
    attention: list[AttentionItem] = Field(default_factory=list, json_schema_extra=ui(hidden=True))


class ProjectIdentity(BaseModel):
    """What discovery and the manifest say a project is."""

    id: str
    name: str
    path: str
    role: ProjectRole
    summary: str | None = None
    kind: str | None = None
    languages: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    machine: str


class UserProjectState(BaseModel):
    """What only the user decides, stored in dashboard state keyed by project id."""

    status: Lifecycle = Lifecycle.ACTIVE
    status_note: str | None = None
    favorite: bool = False
    hidden: bool = False


class ProjectFacts(BaseModel):
    """Everything known about one project, per source; any source may be absent."""

    identity: ProjectIdentity
    user: UserProjectState = Field(default_factory=UserProjectState)
    manifest: ManifestFacts | None = None
    v1: V1Progress | None = None
    git: GitFacts | None = None
    checks: CheckFacts | None = None
    ci: CiFacts | None = None
    issues: IssueFacts | None = None
    services: ServicesFacts | None = None
    fork: ForkFacts | None = None
    standards: StandardsFacts | None = None
    metrics: MetricsFacts | None = None
    agent: AgentFacts | None = None


# --- pure rules ---


def v1_progress(milestones: Sequence[tuple[str, int, bool]], target_date: date | None, today: date) -> V1Progress:
    """Weighted progress over `(title, weight, done)` milestones; the next one is the first not done."""
    total = sum(weight for _title, weight, _done in milestones)
    done = sum(weight for _title, weight, is_done in milestones if is_done)
    next_title = next((title for title, _weight, is_done in milestones if not is_done), None)
    return _progress(
        done=done,
        total=total,
        milestones_done=sum(1 for *_rest, is_done in milestones if is_done),
        milestones_total=len(milestones),
        next_milestone=next_title,
        target_date=target_date,
        today=today,
    )


def github_milestone_progress(open_count: int, closed_count: int, target_date: date | None, today: date) -> V1Progress:
    """Progress = closed / (open + closed) issues in the GitHub milestone."""
    return _progress(
        done=closed_count,
        total=open_count + closed_count,
        milestones_done=closed_count,
        milestones_total=open_count + closed_count,
        next_milestone=None,
        target_date=target_date,
        today=today,
    )


def _progress(
    *,
    done: float,
    total: float,
    milestones_done: int,
    milestones_total: int,
    next_milestone: str | None,
    target_date: date | None,
    today: date,
) -> V1Progress:
    percent = round(100 * done / total, 1) if total else 0.0
    days_left = (target_date - today).days if target_date is not None else None
    caption = f"{milestones_done}/{milestones_total}"
    if days_left is not None:
        caption += f" · {days_left}d left" if days_left >= 0 else f" · {-days_left}d overdue"
    return V1Progress(
        done=done,
        total=total,
        percent=percent,
        milestones_done=milestones_done,
        milestones_total=milestones_total,
        next_milestone=next_milestone,
        target_date=target_date,
        days_left=days_left,
        caption=caption,
        detail=f"next: {next_milestone}" if next_milestone else "",
    )


_FAILED_SERVICE_STATES = frozenset({"failed", "build failed", "blocked"})


def health_reasons(facts: ProjectFacts) -> list[HealthReason]:
    """Every signal that contributes to the health rollup, with its level."""
    reasons: list[HealthReason] = []
    if facts.ci is not None and facts.ci.conclusion != "none":
        if facts.ci.conclusion == "failure":
            workflows = ", ".join(facts.ci.failing_workflows) or "a workflow"
            reasons.append(HealthReason(source="ci", level=HealthLevel.FAILING, message=f"CI failing: {workflows}"))
        elif facts.ci.conclusion == "success":
            reasons.append(
                HealthReason(source="ci", level=HealthLevel.GOOD, message="CI passing on the default branch")
            )
    if facts.checks is not None and facts.checks.total:
        if facts.checks.failed:
            message = f"{facts.checks.failed} of {facts.checks.total} local checks failing"
            reasons.append(HealthReason(source="checks", level=HealthLevel.FAILING, message=message))
        elif facts.checks.stale:
            message = "local checks passed but are stale against HEAD"
            reasons.append(HealthReason(source="checks", level=HealthLevel.WARN, message=message))
        else:
            reasons.append(HealthReason(source="checks", level=HealthLevel.GOOD, message="local checks passing"))
    if facts.manifest is not None and not facts.manifest.valid:
        count = facts.manifest.error_count
        message = f"manifest invalid ({count} error{'s' if count != 1 else ''})"
        reasons.append(HealthReason(source="manifest", level=HealthLevel.FAILING, message=message))
    if facts.standards is not None and facts.standards.total and facts.standards.passing < facts.standards.total:
        failing_count = facts.standards.total - facts.standards.passing
        message = f"{failing_count} of {facts.standards.total} standards checks not met"
        reasons.append(HealthReason(source="standards", level=HealthLevel.WARN, message=message))
    if facts.services is not None:
        crashed = [s.label for s in facts.services.services if s.state in _FAILED_SERVICE_STATES]
        if crashed:
            message = f"service{'s' if len(crashed) != 1 else ''} down: {', '.join(crashed)}"
            reasons.append(HealthReason(source="services", level=HealthLevel.FAILING, message=message))
    return reasons


def rollup(reasons: Iterable[HealthReason]) -> HealthLevel:
    """Worst level wins; without any signal the health is unknown."""
    levels = {reason.level for reason in reasons}
    for level in (HealthLevel.FAILING, HealthLevel.WARN, HealthLevel.GOOD):
        if level in levels:
            return level
    return HealthLevel.UNKNOWN


def last_change(git: GitFacts | None) -> LastChange:
    """The later of the last commit and the newest working-tree change, and which one it is."""
    if git is None:
        return LastChange()
    commit, worktree = git.last_commit_at, git.last_worktree_change_at
    if worktree is not None and (commit is None or worktree > commit):
        return LastChange(at=worktree, source="working-tree")
    if commit is not None:
        return LastChange(at=commit, source="commit")
    return LastChange()


def attention_items(facts: ProjectFacts, health: Health) -> list[AttentionItem]:
    """What needs a decision now: failing health reasons, parked syncs, and a blocked status.

    Warnings (standards drift, checks stale against HEAD) stay in the health
    rollup and their own signals; they are a standing backlog, and counting
    them here would flag nearly every project and rank nothing.
    """
    items = [
        AttentionItem(source=reason.source, level="bad", message=reason.message)
        for reason in health.reasons
        if reason.level is HealthLevel.FAILING
    ]
    if facts.fork is not None and facts.fork.last_sync in ("conflict", "needs-attention", "ready-to-apply", "failed"):
        level: Literal["warn", "bad"] = "bad" if facts.fork.last_sync in ("conflict", "failed") else "warn"
        items.append(AttentionItem(source="fork", level=level, message=f"fork sync {facts.fork.last_sync}"))
    if facts.user.status is Lifecycle.BLOCKED:
        items.append(AttentionItem(source="status", level="warn", message=facts.user.status_note or "marked blocked"))
    return items


def compose_summary(facts: ProjectFacts) -> ProjectSummary:
    """Fold every source's facts into the overview summary."""
    identity = facts.identity
    reasons = health_reasons(facts)
    health = Health(level=rollup(reasons), reasons=reasons)
    services = facts.services or ServicesFacts()
    return ProjectSummary(
        id=identity.id,
        name=identity.name,
        summary=identity.summary,
        role=identity.role,
        kind=identity.kind,
        languages=identity.languages,
        tags=identity.tags,
        path=identity.path,
        machine=identity.machine,
        favorite=facts.user.favorite,
        status=facts.user.status,
        status_note=facts.user.status_note,
        health=health,
        last_change=last_change(facts.git),
        v1=facts.v1,
        checks=facts.checks,
        ci=facts.ci,
        issues=facts.issues,
        git=facts.git,
        fork=facts.fork if facts.fork is not None and facts.fork.is_fork else None,
        standards=facts.standards,
        services=services.services,
        deployments=services.deployments,
        metrics=facts.metrics.pinned if facts.metrics is not None else [],
        agent=facts.agent or AgentFacts(),
        manifest=facts.manifest,
        attention=attention_items(facts, health),
    )
