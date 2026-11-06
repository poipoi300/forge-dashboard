"""GitHub: issues, pull requests, CI runs, releases, and deployments for every project on GitHub.

The service finds each project's repository from its `origin` remote, polls
it in the background (every few minutes, faster while CI runs on the default
branch, and never past the rate-limit reserve), and publishes `IssueFacts`
and `CiFacts` for the overview. A project without a GitHub remote is a normal
state, not an error. On-demand queries serve the project page; the writes
(create an issue, comment, close, label, re-run, open a pull request) exist
only as explicit user actions and refuse to run unauthenticated.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, override

from keyring.errors import KeyringError
from pydantic import BaseModel, Field, JsonValue

from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, Done, NoInput, ProjectRef, Service, action
from forge_dashboard.core.tasks import BackgroundTasks, repeat
from forge_dashboard.core.ui import ui
from forge_dashboard.domain.summary import CiFacts, GitHubLink, IssueFacts
from forge_dashboard.github.auth import (
    AuthSource,
    GitHubAuth,
    forget_token,
    resolve_auth,
    store_token,
)
from forge_dashboard.github.client import (
    GitHubClient,
    GitHubError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    validate_list,
    validate_payload,
)
from forge_dashboard.github.facts import ci_facts, has_active_run, issue_facts, run_verdict
from forge_dashboard.github.fetch import (
    RepoSnapshot,
    fetch_pulls,
    find_milestone,
    pull_checks_rollup,
    take_snapshot,
)
from forge_dashboard.github.models import (
    CloseIssueInput,
    CommentInput,
    CreateIssueInput,
    CreatePullRequestInput,
    GitHubComment,
    GitHubDeployment,
    GitHubDeploymentList,
    GitHubEnvironment,
    GitHubIssue,
    GitHubIssueDetail,
    GitHubIssueList,
    GitHubJob,
    GitHubLabels,
    GitHubPull,
    GitHubPullList,
    GitHubRelease,
    GitHubReleaseList,
    GitHubRepoInfo,
    GitHubRun,
    GitHubRunDetail,
    GitHubRunList,
    GitHubTag,
    GitHubWorkflow,
    IssueListInput,
    IssueRef,
    PullListInput,
    RunListInput,
    RunRef,
    SaveTokenInput,
    SetLabelsInput,
)
from forge_dashboard.github.payloads import (
    CommentPayload,
    DeploymentPayload,
    DeploymentStatusPayload,
    EnvironmentsPayload,
    IssuePayload,
    JobsPayload,
    LabelPayload,
    PullPayload,
    ReleasePayload,
    RepoPayload,
    RunPayload,
    RunsPayload,
    TagPayload,
    WorkflowPayload,
    WorkflowsPayload,
)
from forge_dashboard.github.remote import parse_github_slug
from forge_dashboard.process import tree
from forge_dashboard.services.discovery import ChangeKind
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

GIT_TIMEOUT_S = 10.0
# Shortest sleep between poll rounds, so a mistake in a due time can never spin the loop.
POLL_FLOOR_S = 1.0
CACHE_MAX_AGE_S = 30 * 24 * 3600
# Runs and issues older than this are shown but not announced, so a restart cannot replay old news.
ANNOUNCE_WINDOW_S = 24 * 3600
DEPLOYMENTS_SHOWN = 10
RELEASES_SHOWN = 30

_NO_AUTH = GitHubAuth(AuthSource.NONE, None)
_SOURCE_LABELS = {
    AuthSource.ENVIRONMENT: "GH_TOKEN or GITHUB_TOKEN",
    AuthSource.GH_CLI: "the gh CLI",
    AuthSource.GIT_CREDENTIAL: "the git credential helper",
    AuthSource.KEYRING: "the token saved in the OS keyring",
    AuthSource.NONE: "no token",
}
AUTH_SOURCE_UI = ui(
    format="badge",
    enum_labels={source.value: label for source, label in _SOURCE_LABELS.items()},
    tones={
        "environment": "good",
        "gh-cli": "good",
        "git-credential": "good",
        "keyring": "good",
        "none": "warn",
    },
    short_label="signed in via",
)
LINK_UI = ui(
    format="badge",
    enum_labels={
        "ok": "connected",
        "not-on-github": "not on github",
        "unauthenticated": "sign in",
        "rate-limited": "rate limited",
        "error": "error",
    },
    tones={"ok": "good", "not-on-github": "neutral", "unauthenticated": "warn", "rate-limited": "warn", "error": "bad"},
)


class GitHubConfig(BaseModel):
    idle_poll_s: int = Field(
        300,
        ge=30,
        le=86_400,
        json_schema_extra=ui(widget="duration", group="Polling", order=1, help="How often each repository is checked."),
    )
    active_poll_s: int = Field(
        30,
        ge=10,
        le=3_600,
        json_schema_extra=ui(
            widget="duration",
            group="Polling",
            order=2,
            help="How often it is checked while a workflow is running on its default branch.",
        ),
    )
    rate_reserve_percent: int = Field(
        10,
        ge=0,
        le=50,
        json_schema_extra=ui(
            widget="slider",
            min=0,
            max=50,
            step=1,
            unit="%",
            group="Polling",
            order=3,
            help="Background polling pauses when this share of the hourly budget is left. Your actions keep working.",
        ),
    )
    pull_check_limit: int = Field(
        10,
        ge=0,
        le=100,
        json_schema_extra=ui(
            widget="number",
            advanced=True,
            group="Polling",
            order=4,
            help="Open pull requests whose checks are fetched one by one when no token allows a single GraphQL query.",
        ),
    )


class GitHubRepoState(BaseModel):
    project_id: str
    repo: str | None = Field(None, json_schema_extra=ui(format="code", help="owner/name from the origin remote."))
    link: GitHubLink = Field("not-on-github", json_schema_extra=LINK_UI)
    fetched_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="fetched"))
    detail: str | None = Field(None, json_schema_extra=ui(help="Why the link is not ok, or a remark."))


class GitHubState(BaseModel):
    auth_source: AuthSource = Field(AuthSource.NONE, json_schema_extra=AUTH_SOURCE_UI)
    authenticated: bool = Field(
        False, json_schema_extra=ui(help="Without a token only public repositories work, at 60 requests an hour.")
    )
    rate_limit: int | None = Field(None, json_schema_extra=ui(short_label="limit"))
    rate_remaining: int | None = Field(None, json_schema_extra=ui(short_label="remaining"))
    rate_reset_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="resets"))
    repos: list[GitHubRepoState] = Field(
        default_factory=list, json_schema_extra=ui(columns=["project_id", "repo", "link", "fetched_at", "detail"])
    )


# --- events ---


class CiRunEvent(Event):
    """A finished workflow run on a default branch. Subclasses say what it meant."""

    repo: str
    workflow: str
    run_id: int
    run_number: int
    run_attempt: int
    branch: str
    sha: str
    conclusion: str
    html_url: str
    finished_at: float

    def dedupe_key(self) -> str | None:
        return f"github-run:{self.repo}:{self.run_id}:{self.run_attempt}"

    def occurred_at(self) -> float | None:
        return self.finished_at


class CiFailure(CiRunEvent):
    type: ClassVar[str] = "github.ci-failure"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "ci-failure"

    def title(self) -> str:
        return f"CI failed on {self.branch}: {self.workflow} #{self.run_number}"

    def severity(self) -> Severity:
        return "error"


class CiRunCompleted(CiRunEvent):
    type: ClassVar[str] = "github.ci-run-completed"
    persist: ClassVar[bool] = True

    def title(self) -> str:
        return f"CI {self.conclusion} on {self.branch}: {self.workflow} #{self.run_number}"

    def severity(self) -> Severity:
        return "success" if self.conclusion == "success" else "info"


class IssueOpened(Event):
    type: ClassVar[str] = "github.issue-opened"
    persist: ClassVar[bool] = True
    repo: str
    number: int
    issue_title: str
    author: str | None
    html_url: str
    opened_at: float

    def title(self) -> str:
        return f"Issue #{self.number} opened: {self.issue_title}"

    def dedupe_key(self) -> str | None:
        return f"github-issue:{self.repo}:{self.number}"

    def occurred_at(self) -> float | None:
        return self.opened_at


# --- internals ---


@dataclass
class _Tracked:
    """One project and what is known of its repository."""

    record: ProjectRecord
    slug: str | None
    link: GitHubLink = "not-on-github"
    detail: str | None = None
    fetched_at: float | None = None
    repo: RepoPayload | None = None
    issues: IssueFacts | None = None
    ci: CiFacts | None = None
    ci_active: bool = False
    due_at: float = 0.0  # time.monotonic() of the next poll


def _milestone_title(record: ProjectRecord) -> str | None:
    manifest = record.manifest
    if manifest is None or manifest.v1.source != "github-milestone":
        return None
    return manifest.v1.github_milestone


def _link_for(error: GitHubError, *, authenticated: bool) -> GitHubLink:
    match error:
        case RateLimitedError():
            return "rate-limited"
        case UnauthorizedError():
            return "unauthenticated"
        case NotFoundError() if not authenticated:
            return "unauthenticated"
        case _:
            return "error"


def _describe(error: GitHubError, auth: GitHubAuth) -> str:
    match error:
        case NotFoundError() if auth.authenticated:
            return "not found, or the token cannot read it"
        case NotFoundError():
            return "not found; if the repository is private, sign in to GitHub"
        case UnauthorizedError() if auth.authenticated:
            return f"GitHub rejected the token from {_SOURCE_LABELS[auth.source]}"
        case _:
            return error.message


def _action_status(error: GitHubError) -> int:
    match error:
        case RateLimitedError():
            return 429
        case NotFoundError():
            return 404
        case UnauthorizedError():
            return 409
        case GitHubError(status=422):
            return 422
        case _:
            return 502


class GitHubService(Service[GitHubConfig, GitHubState], ProjectListener):
    id = "github"
    title = "GitHub"
    description = "Issues, pull requests, CI runs, releases, and deployments for projects whose origin is on GitHub."
    Config = GitHubConfig
    State = GitHubState
    events = (CiFailure, CiRunCompleted, IssueOpened)

    def __init__(self, runtime: Runtime, projects: ProjectsService, jobs: JobsService) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._jobs = jobs
        self._tracked: dict[str, _Tracked] = {}
        self._api_client: GitHubClient | None = None
        self._auth_lock = asyncio.Lock()
        self._announced: set[str] = set()
        self._wakeup = asyncio.Event()
        self._background = BackgroundTasks()
        projects.listen(self)

    # --- lifecycle ---

    @override
    async def start(self) -> None:
        self._background.spawn(
            repeat(self._poll_round, self._seconds_until_next_poll, label="GitHub poll", wake=self._wakeup),
            "github-poll",
        )

    @override
    async def stop(self) -> None:
        await self._background.cancel_all()
        client, self._api_client = self._api_client, None  # the next request builds a fresh one
        if client is not None:
            await client.aclose()

    @override
    async def on_config_changed(self, previous: GitHubConfig) -> None:
        if self._api_client is not None:
            self._api_client.reserve_fraction = self.config.rate_reserve_percent / 100
        self._wake()

    # --- the API client and its credential ---

    async def _api(self) -> GitHubClient:
        """The client; the auth chain is resolved the first time it is needed, which may run `gh` and `git`."""
        if self._api_client is not None:
            return self._api_client
        async with self._auth_lock:
            if self._api_client is None:
                client = GitHubClient(
                    self.runtime.db,
                    await resolve_auth(),
                    reserve_fraction=self.config.rate_reserve_percent / 100,
                    on_budget_changed=self._publish_state,
                )
                await client.prune_cache(CACHE_MAX_AGE_S)
                self._api_client = client
                self._publish_state()
            return self._api_client

    async def _resolve_credential(self) -> GitHubAuth:
        """Run the auth chain again, for when the saved token changed or GitHub rejected the credential."""
        client = await self._api()
        client.auth = await resolve_auth()
        self._publish_state()
        return client.auth

    def repo_info(self, project_id: str) -> GitHubRepoInfo | None:
        """The repository metadata from the latest poll; None before the first one or when not on GitHub."""
        tracked = self._tracked.get(project_id)
        if tracked is None or tracked.repo is None:
            return None
        return GitHubRepoInfo.from_payload(tracked.repo)

    # --- following the project registry ---

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        # The manifest may have changed, and with it the milestone to count.
        await self._track(record, await self._origin_slug(record), repoll=True)

    @override
    async def project_removed(self, project_id: str) -> None:
        self._tracked.pop(project_id, None)
        self._publish_state()

    @override
    async def files_changed(self, record: ProjectRecord, kinds: frozenset[ChangeKind]) -> None:
        if not kinds & {"git", "manifest"}:
            return
        # A manifest change (or a manual refresh, which reports one) may name another milestone; a git change alone
        # only matters when the origin moved.
        await self._track(record, await self._origin_slug(record), repoll="manifest" in kinds)

    async def _origin_slug(self, record: ProjectRecord) -> str | None:
        if not record.is_git:
            return None
        completed = await tree.run(
            ["git", "-C", str(record.path), "remote", "get-url", "origin"], record.path, timeout_s=GIT_TIMEOUT_S
        )
        return parse_github_slug(completed.stdout) if completed.ok else None

    async def _track(self, record: ProjectRecord, slug: str | None, *, repoll: bool) -> None:
        """Start following a project, or update it; a new repository is polled at once, an unchanged one when asked."""
        previous = self._tracked.get(record.id)
        if previous is not None and previous.slug == slug:
            previous.record = record
            if repoll:
                self._wake(record.id)
        else:
            self._tracked[record.id] = _Tracked(record=record, slug=slug)
            self._wakeup.set()
            if slug is None:
                self._projects.put(record.id, IssueFacts(github="not-on-github"))
                if previous is not None and previous.ci is not None:
                    self._projects.put(record.id, CiFacts())
        self._publish_state()

    # --- polling ---

    async def _poll_round(self) -> None:
        await self._api()
        await self.poll_due()

    async def poll_due(self) -> list[str]:
        """Refresh every project whose poll time has come; returns their ids."""
        now = time.monotonic()
        due = [t.record.id for t in list(self._tracked.values()) if t.slug is not None and t.due_at <= now]
        for project_id in due:
            try:
                await self.refresh(project_id)
            except Exception:  # one repository's bug must not stop the others from being polled
                self.runtime.log.exception("GitHub poll of %s failed", project_id)
        return due

    def _seconds_until_next_poll(self) -> float:
        now = time.monotonic()
        delay = min(
            (t.due_at - now for t in self._tracked.values() if t.slug is not None), default=self.config.idle_poll_s
        )
        return max(delay, POLL_FLOOR_S)

    def _wake(self, project_id: str | None = None) -> None:
        """Poll one project (or all) as soon as the loop is free."""
        for tracked in self._tracked.values():
            if project_id is None or tracked.record.id == project_id:
                tracked.due_at = 0.0
        self._wakeup.set()

    async def refresh(self, project_id: str) -> None:
        """Poll one project's repository now and publish its facts."""
        tracked = self._tracked.get(project_id)
        if tracked is None or tracked.slug is None:
            return
        tracked.due_at = time.monotonic() + self.config.idle_poll_s  # a failure below must not leave it due at once
        api = await self._api()
        rate_limit_wait_s = 0.0
        try:
            snapshot = await take_snapshot(
                api,
                tracked.slug,
                milestone_title=_milestone_title(tracked.record),
                pull_check_limit=self.config.pull_check_limit,
            )
        except GitHubError as error:
            await self._record_failure(tracked, tracked.slug, api.auth, error)
            if isinstance(error, RateLimitedError) and error.reset_at is not None:
                rate_limit_wait_s = error.reset_at - time.time()
        else:
            self._record_snapshot(tracked, tracked.slug, snapshot)
        interval_s = self.config.active_poll_s if tracked.ci_active else self.config.idle_poll_s
        tracked.due_at = time.monotonic() + max(interval_s, rate_limit_wait_s)
        self._publish_state()

    def _record_snapshot(self, tracked: _Tracked, slug: str, snapshot: RepoSnapshot) -> None:
        milestone_title = _milestone_title(tracked.record)
        tracked.repo = snapshot.repo
        tracked.issues = issue_facts(
            slug,
            snapshot.repo,
            open_issues=snapshot.open_issues,
            open_prs=snapshot.open_prs,
            listed=snapshot.listed_issues,
            milestone=snapshot.milestone,
        )
        tracked.ci = ci_facts(snapshot.workflows, snapshot.latest_runs, pull_checks_rollup(snapshot.pulls))
        tracked.ci_active = has_active_run(snapshot.latest_runs)
        tracked.link = "ok"
        tracked.fetched_at = time.time()
        tracked.detail = (
            f"GitHub milestone {milestone_title!r} not found"
            if milestone_title is not None and snapshot.milestone is None
            else None
        )
        self._projects.put(tracked.record.id, tracked.issues)
        self._projects.put(tracked.record.id, tracked.ci)
        self._announce(tracked, snapshot)

    async def _record_failure(self, tracked: _Tracked, slug: str, auth: GitHubAuth, error: GitHubError) -> None:
        tracked.link = _link_for(error, authenticated=auth.authenticated)
        tracked.detail = _describe(error, auth)
        previous = tracked.issues or IssueFacts(repo=slug)
        self._projects.put(tracked.record.id, previous.model_copy(update={"github": tracked.link}))
        if isinstance(error, UnauthorizedError) and auth.authenticated:
            await self._resolve_credential()  # a rotated credential may be accepted by the next poll

    def _announce(self, tracked: _Tracked, snapshot: RepoSnapshot) -> None:
        """Publish an event for each finished default-branch run and each new issue, once per process."""
        cutoff = time.time() - ANNOUNCE_WINDOW_S
        repo = snapshot.repo.full_name
        workflow_names = {workflow.id: workflow.name for workflow in snapshot.workflows}
        for run in snapshot.latest_runs:
            verdict = run_verdict(run)
            finished_at = run.updated_at.timestamp()
            if verdict == "active" or not self._first_sight(f"run:{repo}:{run.id}:{run.run_attempt}"):
                continue
            if finished_at < cutoff:
                continue
            event_type = CiFailure if verdict == "failed" else CiRunCompleted
            self.publish(
                event_type(
                    project_id=tracked.record.id,
                    repo=repo,
                    workflow=workflow_names[run.workflow_id],
                    run_id=run.id,
                    run_number=run.run_number,
                    run_attempt=run.run_attempt,
                    branch=run.head_branch or snapshot.repo.default_branch,
                    sha=run.head_sha[:10],
                    conclusion=run.conclusion or "unknown",
                    html_url=run.html_url,
                    finished_at=finished_at,
                )
            )
        for issue in snapshot.listed_issues:
            if issue.is_pull_request or not self._first_sight(f"issue:{repo}:{issue.number}"):
                continue
            if issue.created_at.timestamp() < cutoff:
                continue
            self.publish(
                IssueOpened(
                    project_id=tracked.record.id,
                    repo=repo,
                    number=issue.number,
                    issue_title=issue.title,
                    author=issue.user.login if issue.user is not None else None,
                    html_url=issue.html_url,
                    opened_at=issue.created_at.timestamp(),
                )
            )

    def _first_sight(self, key: str) -> bool:
        if key in self._announced:
            return False
        self._announced.add(key)
        return True

    def _publish_state(self) -> None:
        auth = self._api_client.auth if self._api_client is not None else _NO_AUTH
        budget = self._api_client.budget if self._api_client is not None else None
        self.set_state(
            GitHubState(
                auth_source=auth.source,
                authenticated=auth.authenticated,
                rate_limit=budget.limit if budget is not None else None,
                rate_remaining=budget.remaining if budget is not None else None,
                rate_reset_at=budget.reset_at if budget is not None else None,
                repos=[
                    GitHubRepoState(
                        project_id=tracked.record.id,
                        repo=tracked.slug,
                        link=tracked.link,
                        fetched_at=tracked.fetched_at,
                        detail=tracked.detail,
                    )
                    for tracked in sorted(self._tracked.values(), key=lambda t: t.record.id)
                ],
            )
        )

    # --- helpers for actions ---

    def _repository(self, project_id: str) -> str:
        tracked = self._tracked.get(project_id)
        if tracked is None:
            raise ActionError(f"no project {project_id!r}", status=404)
        if tracked.slug is None:
            raise ActionError(f"{project_id} is not on GitHub: its origin remote is not a github.com repository", 409)
        return tracked.slug

    async def _reader(self, project_id: str) -> tuple[GitHubClient, str]:
        slug = self._repository(project_id)
        return await self._api(), slug

    async def _writer(self, project_id: str) -> tuple[GitHubClient, str]:
        api, slug = await self._reader(project_id)
        if not api.auth.authenticated:
            raise ActionError(
                "GitHub writes need a token: set GH_TOKEN, sign in with gh or git, or save a token here", status=409
            )
        return api, slug

    @contextlib.contextmanager
    def _github_errors(self) -> Iterator[None]:
        """Turn a failed GitHub request into the `ActionError` the API reports."""
        try:
            yield
        except GitHubError as error:
            auth = self._api_client.auth if self._api_client is not None else _NO_AUTH
            raise ActionError(_describe(error, auth), status=_action_status(error)) from error

    # --- queries ---

    @action(title="Repository", query=True)
    async def repo(self, request: ProjectRef) -> GitHubRepoInfo:
        """Repository metadata: default branch, visibility, fork parent."""
        api, slug = await self._reader(request.project_id)
        with self._github_errors():
            return GitHubRepoInfo.from_payload(await api.get_model(f"/repos/{slug}", RepoPayload, essential=True))

    @action(title="Issues", query=True)
    async def issues(self, request: IssueListInput) -> GitHubIssueList:
        """Issues (not pull requests), filtered by state, labels, assignee, and milestone."""
        api, slug = await self._reader(request.project_id)
        params: dict[str, str | int | bool] = {"state": request.state, "sort": "updated", "direction": "desc"}
        if request.labels:
            params["labels"] = ",".join(request.labels)
        if request.assignee:
            params["assignee"] = request.assignee
        with self._github_errors():
            if request.milestone:
                milestone = await find_milestone(api, slug, request.milestone, essential=True)
                if milestone is None:
                    raise ActionError(f"{slug} has no milestone titled {request.milestone!r}", status=404)
                params["milestone"] = milestone.number
            page = await api.get_pages(f"/repos/{slug}/issues", IssuePayload, params, essential=True)
        issues = [GitHubIssue.from_payload(item) for item in page.items if not item.is_pull_request]
        return GitHubIssueList(
            repo=slug, issues=issues[: request.limit], truncated=page.more_available or len(issues) > request.limit
        )

    @action(title="Issue", query=True)
    async def issue(self, request: IssueRef) -> GitHubIssueDetail:
        """One issue with its markdown body and comments."""
        api, slug = await self._reader(request.project_id)
        with self._github_errors():
            payload = await api.get_model(f"/repos/{slug}/issues/{request.number}", IssuePayload, essential=True)
            comments = await api.get_pages(
                f"/repos/{slug}/issues/{request.number}/comments", CommentPayload, essential=True
            )
        return GitHubIssueDetail(
            issue=GitHubIssue.from_payload(payload),
            body=payload.body or "",
            comments=[GitHubComment.from_payload(comment) for comment in comments.items],
            comments_truncated=comments.more_available,
        )

    @action(title="Pull requests", query=True)
    async def pulls(self, request: PullListInput) -> GitHubPullList:
        """Pull requests with their check status."""
        api, slug = await self._reader(request.project_id)
        with self._github_errors():
            listing = await fetch_pulls(
                api, slug, request.state, limit=request.limit, check_limit=request.limit, essential=True
            )
        return GitHubPullList(repo=slug, pulls=listing.pulls)

    @action(title="Workflow runs", query=True)
    async def runs(self, request: RunListInput) -> GitHubRunList:
        """Recent Actions runs, optionally for one branch."""
        api, slug = await self._reader(request.project_id)
        params: dict[str, str | int | bool] = {"per_page": request.limit}
        if request.branch:
            params["branch"] = request.branch
        with self._github_errors():
            workflows = await api.get_model(
                f"/repos/{slug}/actions/workflows", WorkflowsPayload, {"per_page": 100}, essential=True
            )
            listing = await api.get_model(f"/repos/{slug}/actions/runs", RunsPayload, params, essential=True)
        names = {workflow.id: workflow.name for workflow in workflows.workflows}
        return GitHubRunList(
            repo=slug,
            workflows=[GitHubWorkflow.from_payload(workflow) for workflow in workflows.workflows],
            runs=[GitHubRun.from_payload(run, names) for run in listing.workflow_runs],
        )

    @action(title="Run details", query=True)
    async def run(self, request: RunRef) -> GitHubRunDetail:
        """One run's jobs and steps, with the log page of each job."""
        api, slug = await self._reader(request.project_id)
        with self._github_errors():
            payload = await api.get_model(f"/repos/{slug}/actions/runs/{request.run_id}", RunPayload, essential=True)
            # A run's own `name` is its run-name when the workflow sets one, so the workflow is asked for its name.
            workflow = await api.get_model(
                f"/repos/{slug}/actions/workflows/{payload.workflow_id}", WorkflowPayload, essential=True
            )
            jobs = await api.get_model(
                f"/repos/{slug}/actions/runs/{request.run_id}/jobs", JobsPayload, {"per_page": 100}, essential=True
            )
        return GitHubRunDetail(
            run=GitHubRun.from_payload(payload, {workflow.id: workflow.name}),
            jobs=[GitHubJob.from_payload(job) for job in jobs.jobs],
        )

    @action(title="Releases and tags", query=True)
    async def releases(self, request: ProjectRef) -> GitHubReleaseList:
        """Releases and tags, newest first (commits since the last tag are the git service's)."""
        api, slug = await self._reader(request.project_id)
        params = {"per_page": RELEASES_SHOWN}
        with self._github_errors():
            releases = await api.get_models(f"/repos/{slug}/releases", ReleasePayload, params, essential=True)
            tags = await api.get_models(f"/repos/{slug}/tags", TagPayload, params, essential=True)
        return GitHubReleaseList(
            repo=slug,
            releases=[GitHubRelease.from_payload(release) for release in releases],
            tags=[GitHubTag.from_payload(tag) for tag in tags],
        )

    @action(title="Deployments", query=True)
    async def deployments(self, request: ProjectRef) -> GitHubDeploymentList:
        """Environments and the latest deployments with their current status."""
        api, slug = await self._reader(request.project_id)
        with self._github_errors():
            environments = await api.get_model(f"/repos/{slug}/environments", EnvironmentsPayload, essential=True)
            deployments = await api.get_models(
                f"/repos/{slug}/deployments", DeploymentPayload, {"per_page": DEPLOYMENTS_SHOWN}, essential=True
            )
            shown: list[GitHubDeployment] = []
            for deployment in deployments:
                statuses = await api.get_models(
                    f"/repos/{slug}/deployments/{deployment.id}/statuses",
                    DeploymentStatusPayload,
                    {"per_page": 1},
                    essential=True,
                )
                latest = statuses[0] if statuses else None
                shown.append(
                    GitHubDeployment.from_payload(
                        deployment,
                        latest.state if latest is not None else None,
                        latest.environment_url if latest is not None else None,
                    )
                )
        return GitHubDeploymentList(
            repo=slug,
            environments=[GitHubEnvironment.from_payload(item) for item in environments.environments],
            deployments=shown,
        )

    # --- writes: only ever a user's explicit action ---

    @action(title="Refresh GitHub data")
    async def refresh_now(self, request: ProjectRef) -> Done:
        """Poll this project's repository again as soon as possible."""
        self._repository(request.project_id)
        self._wake(request.project_id)
        return Done(message="refreshing")

    @action(title="Create issue")
    async def create_issue(self, request: CreateIssueInput) -> GitHubIssue:
        """Open a new issue."""
        api, slug = await self._writer(request.project_id)
        path = f"/repos/{slug}/issues"
        body: dict[str, JsonValue] = {"title": request.title, "body": request.body, "labels": [*request.labels]}
        with self._github_errors():
            created = validate_payload(path, IssuePayload, await api.post(path, body))
        self._wake(request.project_id)
        return GitHubIssue.from_payload(created)

    @action(title="Comment")
    async def comment(self, request: CommentInput) -> GitHubComment:
        """Add a comment to an issue or pull request."""
        api, slug = await self._writer(request.project_id)
        path = f"/repos/{slug}/issues/{request.number}/comments"
        with self._github_errors():
            created = validate_payload(path, CommentPayload, await api.post(path, {"body": request.body}))
        return GitHubComment.from_payload(created)

    @action(title="Close issue", confirm=True)
    async def close_issue(self, request: CloseIssueInput) -> GitHubIssue:
        """Close an issue as completed or not planned."""
        return await self._set_issue_state(request, {"state": "closed", "state_reason": request.reason})

    @action(title="Reopen issue")
    async def reopen_issue(self, request: IssueRef) -> GitHubIssue:
        """Reopen a closed issue."""
        return await self._set_issue_state(request, {"state": "open", "state_reason": "reopened"})

    async def _set_issue_state(self, request: IssueRef, changes: dict[str, JsonValue]) -> GitHubIssue:
        api, slug = await self._writer(request.project_id)
        path = f"/repos/{slug}/issues/{request.number}"
        with self._github_errors():
            updated = validate_payload(path, IssuePayload, await api.patch(path, changes))
        self._wake(request.project_id)
        return GitHubIssue.from_payload(updated)

    @action(title="Set labels")
    async def set_labels(self, request: SetLabelsInput) -> GitHubLabels:
        """Replace every label on an issue or pull request."""
        api, slug = await self._writer(request.project_id)
        path = f"/repos/{slug}/issues/{request.number}/labels"
        with self._github_errors():
            applied = validate_list(path, LabelPayload, await api.put(path, {"labels": [*request.labels]}))
        self._wake(request.project_id)
        return GitHubLabels(number=request.number, labels=[label.name for label in applied])

    @action(title="Re-run failed jobs", confirm=True)
    async def rerun_failed_jobs(self, request: RunRef) -> Done:
        """Re-run only the failed jobs of a workflow run."""
        api, slug = await self._writer(request.project_id)
        with self._github_errors():
            await api.post(f"/repos/{slug}/actions/runs/{request.run_id}/rerun-failed-jobs", {})
        self._wake(request.project_id)
        return Done(message=f"re-running the failed jobs of run {request.run_id}")

    @action(title="Create pull request")
    async def create_pull_request(self, request: CreatePullRequestInput) -> GitHubPull:
        """Open a pull request from a pushed branch."""
        api, slug = await self._writer(request.project_id)
        with self._github_errors():
            base = request.base or (await api.get_model(f"/repos/{slug}", RepoPayload, essential=True)).default_branch
            body: dict[str, JsonValue] = {
                "title": request.title,
                "head": request.head,
                "base": base,
                "body": request.body,
                "draft": request.draft,
            }
            created = validate_payload("/pulls", PullPayload, await api.post(f"/repos/{slug}/pulls", body))
        self._wake(request.project_id)
        return GitHubPull.from_payload(created, None)

    # --- the token the user saves ---

    @action(title="Save GitHub token")
    async def save_token(self, request: SaveTokenInput) -> Done:
        """Keep a personal access token in the OS keyring, for when no other source provides one."""
        token = request.token.get_secret_value().strip()
        if not token:
            raise ActionError("the token is empty", status=422)
        try:
            await store_token(token)
        except KeyringError as error:
            raise ActionError(f"the OS keyring could not store the token ({type(error).__name__})", 500) from error
        auth = await self._resolve_credential()
        self._wake()
        return Done(message=f"token saved; GitHub access now uses {_SOURCE_LABELS[auth.source]}")

    @action(title="Forget saved GitHub token", confirm=True)
    async def clear_token(self, _request: NoInput) -> Done:
        """Remove the token saved in the OS keyring."""
        try:
            removed = await forget_token()
        except KeyringError as error:
            raise ActionError(f"the OS keyring could not remove the token ({type(error).__name__})", 500) from error
        auth = await self._resolve_credential()
        self._wake()
        removal = "saved token removed" if removed else "no saved token"
        return Done(message=f"{removal}; GitHub access now uses {_SOURCE_LABELS[auth.source]}")
