"""Typed inputs and outputs of the GitHub service's actions.

Outputs are the dashboard's own shapes, built from the payloads in
`payloads.py`: timestamps are epoch seconds, users are logins, labels are
names. Every input that targets a repository extends `ProjectRef`, so the UI
fills the project from the page it is on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from forge_dashboard.core.service import ProjectRef
from forge_dashboard.core.ui import ui, with_hints
from forge_dashboard.github.facts import ChecksState, run_duration_s, run_outcome, run_started_at
from forge_dashboard.github.payloads import (
    CommentPayload,
    DeploymentPayload,
    EnvironmentPayload,
    IssuePayload,
    JobPayload,
    PullNodePayload,
    PullPayload,
    ReleasePayload,
    RepoPayload,
    RunPayload,
    StepPayload,
    TagPayload,
    WorkflowPayload,
)

OUTCOME_UI = ui(
    format="badge",
    tones={
        "success": "good",
        "failure": "bad",
        "timed_out": "bad",
        "startup_failure": "bad",
        "action_required": "warn",
        "cancelled": "neutral",
        "skipped": "neutral",
        "neutral": "neutral",
        "stale": "neutral",
        "in_progress": "info",
        "queued": "info",
        "waiting": "info",
        "pending": "info",
        "requested": "info",
    },
)
CHECKS_UI = ui(
    format="badge",
    tones={"passing": "good", "failing": "bad", "pending": "info", "none": "neutral"},
    short_label="checks",
)
TIME_UI = ui(format="relative-time")
URL_UI = ui(format="url", hidden=True)

# --- inputs ---


class IssueListInput(ProjectRef):
    state: Literal["open", "closed", "all"] = Field("open", json_schema_extra=ui(widget="select"))
    labels: list[str] = Field(
        default_factory=list, json_schema_extra=ui(widget="tags", help="Issues with all of these.")
    )
    assignee: str | None = Field(None, json_schema_extra=ui(placeholder="login, or none"))
    milestone: str | None = Field(None, json_schema_extra=ui(placeholder="milestone title"))
    limit: int = Field(50, ge=1, le=200, json_schema_extra=ui(advanced=True))


class IssueRef(ProjectRef):
    number: int = Field(ge=1, json_schema_extra=ui(placeholder="#"))


class PullListInput(ProjectRef):
    state: Literal["open", "closed", "all"] = Field("open", json_schema_extra=ui(widget="select"))
    limit: int = Field(30, ge=1, le=100, json_schema_extra=ui(advanced=True))


class RunListInput(ProjectRef):
    branch: str | None = Field(None, json_schema_extra=ui(placeholder="every branch"))
    limit: int = Field(20, ge=1, le=100, json_schema_extra=ui(advanced=True))


class RunRef(ProjectRef):
    run_id: int = Field(ge=1)


class CreateIssueInput(ProjectRef):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field("", json_schema_extra=ui(widget="textarea", placeholder="Markdown"))
    labels: list[str] = Field(default_factory=list, json_schema_extra=ui(widget="tags"))


class CommentInput(IssueRef):
    body: str = Field(min_length=1, json_schema_extra=ui(widget="textarea", placeholder="Markdown"))


class CloseIssueInput(IssueRef):
    reason: Literal["completed", "not_planned"] = Field(
        "completed",
        json_schema_extra=ui(widget="select", enum_labels={"completed": "completed", "not_planned": "not planned"}),
    )


class SetLabelsInput(IssueRef):
    labels: list[str] = Field(
        default_factory=list, json_schema_extra=ui(widget="tags", help="Replaces every label on the issue.")
    )


class CreatePullRequestInput(ProjectRef):
    head: str = Field(min_length=1, json_schema_extra=ui(placeholder="branch, or owner:branch"))
    base: str | None = Field(None, json_schema_extra=ui(placeholder="the default branch"))
    title: str = Field(min_length=1, max_length=256)
    body: str = Field("", json_schema_extra=ui(widget="textarea", placeholder="Markdown"))
    draft: bool = Field(False, json_schema_extra=ui(widget="toggle"))


class SaveTokenInput(BaseModel):
    token: SecretStr = Field(
        min_length=1,
        json_schema_extra=ui(widget="secret", help="A personal access token. It is kept in the OS keyring."),
    )


# --- outputs ---


class GitHubRepoInfo(BaseModel):
    repo: str
    html_url: str = Field(json_schema_extra=ui(format="url"))
    description: str | None = None
    default_branch: str = Field(json_schema_extra=ui(format="code"))
    private: bool
    fork: bool
    parent: str | None = Field(None, json_schema_extra=ui(help="The repository this one was forked from."))
    archived: bool = False

    @classmethod
    def from_payload(cls, repo: RepoPayload) -> GitHubRepoInfo:
        return cls(
            repo=repo.full_name,
            html_url=repo.html_url,
            description=repo.description,
            default_branch=repo.default_branch,
            private=repo.private,
            fork=repo.fork,
            parent=repo.parent.full_name if repo.parent is not None else None,
            archived=repo.archived,
        )


class GitHubIssue(BaseModel):
    number: int
    title: str
    state: str = Field(json_schema_extra=ui(format="badge", tones={"open": "good", "closed": "neutral"}))
    author: str | None = None
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    milestone: str | None = None
    comments: int = 0
    created_at: float = Field(json_schema_extra=TIME_UI)
    updated_at: float = Field(json_schema_extra=TIME_UI)
    html_url: str = Field(json_schema_extra=URL_UI)

    @classmethod
    def from_payload(cls, issue: IssuePayload) -> GitHubIssue:
        return cls(
            number=issue.number,
            title=issue.title,
            state=issue.state,
            author=issue.user.login if issue.user is not None else None,
            labels=issue.label_names,
            assignees=[user.login for user in issue.assignees],
            milestone=issue.milestone.title if issue.milestone is not None else None,
            comments=issue.comments,
            created_at=issue.created_at.timestamp(),
            updated_at=issue.updated_at.timestamp(),
            html_url=issue.html_url,
        )


class GitHubIssueList(BaseModel):
    repo: str
    issues: list[GitHubIssue] = Field(
        json_schema_extra=ui(columns=["number", "title", "state", "labels", "assignees", "comments", "updated_at"])
    )
    truncated: bool = Field(False, json_schema_extra=ui(help="More issues match than were listed."))


class GitHubComment(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    author: str | None = None
    body: str = Field(json_schema_extra=ui(widget="textarea"))
    created_at: float = Field(json_schema_extra=TIME_UI)
    html_url: str = Field(json_schema_extra=URL_UI)

    @classmethod
    def from_payload(cls, comment: CommentPayload) -> GitHubComment:
        return cls(
            id=comment.id,
            author=comment.user.login if comment.user is not None else None,
            body=comment.body or "",
            created_at=comment.created_at.timestamp(),
            html_url=comment.html_url,
        )


class GitHubIssueDetail(BaseModel):
    issue: GitHubIssue
    body: str = Field(json_schema_extra=ui(widget="textarea", help="Markdown."))
    comments: list[GitHubComment]
    comments_truncated: bool = False


class GitHubLabels(BaseModel):
    number: int
    labels: list[str]


class GitHubPull(BaseModel):
    number: int
    title: str
    author: str | None = None
    draft: bool = False
    head_branch: str = Field(json_schema_extra=ui(format="code"))
    base_branch: str = Field(json_schema_extra=ui(format="code"))
    head_sha: str = Field(json_schema_extra=ui(hidden=True))
    checks: ChecksState | None = Field(None, json_schema_extra=with_hints(CHECKS_UI, help="Empty when not fetched."))
    created_at: float = Field(json_schema_extra=TIME_UI)
    updated_at: float = Field(json_schema_extra=TIME_UI)
    html_url: str = Field(json_schema_extra=URL_UI)

    @classmethod
    def from_payload(cls, pull: PullPayload, checks: ChecksState | None) -> GitHubPull:
        return cls(
            number=pull.number,
            title=pull.title,
            author=pull.user.login if pull.user is not None else None,
            draft=pull.draft,
            head_branch=pull.head.ref,
            base_branch=pull.base.ref,
            head_sha=pull.head.sha,
            checks=checks,
            created_at=pull.created_at.timestamp(),
            updated_at=pull.updated_at.timestamp(),
            html_url=pull.html_url,
        )

    @classmethod
    def from_node(cls, node: PullNodePayload, checks: ChecksState) -> GitHubPull:
        return cls(
            number=node.number,
            title=node.title,
            author=node.author.login if node.author is not None else None,
            draft=node.is_draft,
            head_branch=node.head_ref_name,
            base_branch=node.base_ref_name,
            head_sha=node.head_ref_oid,
            checks=checks,
            created_at=node.created_at.timestamp(),
            updated_at=node.updated_at.timestamp(),
            html_url=node.url,
        )


class GitHubPullList(BaseModel):
    repo: str
    pulls: list[GitHubPull] = Field(
        json_schema_extra=ui(columns=["number", "title", "author", "head_branch", "checks", "updated_at"])
    )


class GitHubWorkflow(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    name: str
    path: str = Field(json_schema_extra=ui(format="code"))
    state: str

    @classmethod
    def from_payload(cls, workflow: WorkflowPayload) -> GitHubWorkflow:
        return cls(id=workflow.id, name=workflow.name, path=workflow.path, state=workflow.state)


class GitHubRun(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    workflow: str
    workflow_id: int = Field(json_schema_extra=ui(hidden=True))
    number: int
    attempt: int = 1
    event: str
    branch: str | None = Field(None, json_schema_extra=ui(format="code"))
    sha: str = Field(json_schema_extra=ui(format="code"))
    title: str | None = None
    outcome: str = Field(json_schema_extra=with_hints(OUTCOME_UI, short_label="result"))
    started_at: float = Field(json_schema_extra=ui(format="relative-time", short_label="started"))
    duration_s: float | None = Field(None, json_schema_extra=ui(format="duration", short_label="took"))
    html_url: str = Field(json_schema_extra=ui(format="url", short_label="open"))

    @classmethod
    def from_payload(cls, run: RunPayload, workflow_names: dict[int, str]) -> GitHubRun:
        return cls(
            id=run.id,
            workflow=workflow_names.get(run.workflow_id) or run.name or f"workflow {run.workflow_id}",
            workflow_id=run.workflow_id,
            number=run.run_number,
            attempt=run.run_attempt,
            event=run.event,
            branch=run.head_branch,
            sha=run.head_sha[:10],
            title=run.display_title,
            outcome=run_outcome(run.status, run.conclusion),
            started_at=run_started_at(run),
            duration_s=run_duration_s(run),
            html_url=run.html_url,
        )


class GitHubRunList(BaseModel):
    repo: str
    workflows: list[GitHubWorkflow]
    runs: list[GitHubRun] = Field(
        json_schema_extra=ui(columns=["workflow", "outcome", "branch", "event", "started_at", "duration_s"])
    )


class GitHubStep(BaseModel):
    number: int
    name: str
    outcome: str = Field(json_schema_extra=OUTCOME_UI)

    @classmethod
    def from_payload(cls, step: StepPayload) -> GitHubStep:
        return cls(number=step.number, name=step.name, outcome=run_outcome(step.status, step.conclusion))


class GitHubJob(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    name: str
    outcome: str = Field(json_schema_extra=OUTCOME_UI)
    started_at: float | None = Field(None, json_schema_extra=TIME_UI)
    duration_s: float | None = Field(None, json_schema_extra=ui(format="duration"))
    logs_url: str | None = Field(None, json_schema_extra=ui(format="url", help="The job's log page on GitHub."))
    steps: list[GitHubStep] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, job: JobPayload) -> GitHubJob:
        duration = (
            (job.completed_at - job.started_at).total_seconds()
            if job.started_at is not None and job.completed_at is not None
            else None
        )
        return cls(
            id=job.id,
            name=job.name,
            outcome=run_outcome(job.status, job.conclusion),
            started_at=job.started_at.timestamp() if job.started_at is not None else None,
            duration_s=max(duration, 0.0) if duration is not None else None,
            logs_url=job.html_url,
            steps=[GitHubStep.from_payload(step) for step in job.steps],
        )


class GitHubRunDetail(BaseModel):
    run: GitHubRun
    jobs: list[GitHubJob]


class GitHubRelease(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    tag: str = Field(json_schema_extra=ui(format="code"))
    name: str | None = None
    draft: bool = False
    prerelease: bool = False
    published_at: float | None = Field(None, json_schema_extra=TIME_UI)
    body: str | None = Field(None, json_schema_extra=ui(hidden=True))
    html_url: str = Field(json_schema_extra=URL_UI)

    @classmethod
    def from_payload(cls, release: ReleasePayload) -> GitHubRelease:
        published = release.published_at or release.created_at
        return cls(
            id=release.id,
            tag=release.tag_name,
            name=release.name,
            draft=release.draft,
            prerelease=release.prerelease,
            published_at=None if release.draft else published.timestamp(),
            body=release.body,
            html_url=release.html_url,
        )


class GitHubTag(BaseModel):
    name: str = Field(json_schema_extra=ui(format="code"))
    sha: str = Field(json_schema_extra=ui(format="code"))

    @classmethod
    def from_payload(cls, tag: TagPayload) -> GitHubTag:
        return cls(name=tag.name, sha=tag.commit.sha[:10])


class GitHubReleaseList(BaseModel):
    repo: str
    releases: list[GitHubRelease] = Field(json_schema_extra=ui(columns=["tag", "name", "published_at", "prerelease"]))
    tags: list[GitHubTag] = Field(json_schema_extra=ui(columns=["name", "sha"]))


class GitHubEnvironment(BaseModel):
    name: str
    html_url: str | None = Field(None, json_schema_extra=ui(format="url"))

    @classmethod
    def from_payload(cls, environment: EnvironmentPayload) -> GitHubEnvironment:
        return cls(name=environment.name, html_url=environment.html_url)


class GitHubDeployment(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    environment: str
    ref: str = Field(json_schema_extra=ui(format="code"))
    sha: str = Field(json_schema_extra=ui(format="code"))
    description: str | None = None
    creator: str | None = None
    created_at: float = Field(json_schema_extra=TIME_UI)
    state: str | None = Field(
        None,
        json_schema_extra=ui(
            format="badge",
            tones={"success": "good", "failure": "bad", "error": "bad", "in_progress": "info", "queued": "info"},
            help="The latest deployment status.",
        ),
    )
    environment_url: str | None = Field(None, json_schema_extra=ui(format="url"))

    @classmethod
    def from_payload(
        cls, deployment: DeploymentPayload, state: str | None, environment_url: str | None
    ) -> GitHubDeployment:
        return cls(
            id=deployment.id,
            environment=deployment.environment,
            ref=deployment.ref,
            sha=deployment.sha[:10],
            description=deployment.description,
            creator=deployment.creator.login if deployment.creator is not None else None,
            created_at=deployment.created_at.timestamp(),
            state=state,
            environment_url=environment_url,
        )


class GitHubDeploymentList(BaseModel):
    repo: str
    environments: list[GitHubEnvironment]
    deployments: list[GitHubDeployment] = Field(
        json_schema_extra=ui(columns=["environment", "state", "ref", "creator", "created_at"])
    )
