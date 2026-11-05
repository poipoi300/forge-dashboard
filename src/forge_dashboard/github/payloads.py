"""The parts of GitHub's REST and GraphQL responses the dashboard reads.

Each model names only the fields in use and ignores the rest. Fields GitHub
documents as nullable are optional; open-ended vocabularies (run status,
check conclusions) stay plain strings, so a value GitHub adds later is
handled by the rules in `facts.py` instead of failing validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True)


class UserPayload(Payload):
    login: str


class LabelPayload(Payload):
    name: str


class MilestonePayload(Payload):
    number: int
    title: str
    state: str
    open_issues: int
    closed_issues: int


class ParentRepoPayload(Payload):
    full_name: str


class RepoPayload(Payload):
    full_name: str
    html_url: str
    description: str | None = None
    default_branch: str
    private: bool
    fork: bool
    archived: bool = False
    open_issues_count: int
    parent: ParentRepoPayload | None = None


class IssuePayload(Payload):
    """An item of the issues endpoint, which lists pull requests too (they carry `pull_request`)."""

    number: int
    title: str
    state: str
    body: str | None = None
    user: UserPayload | None = None
    html_url: str
    labels: list[LabelPayload | str] = Field(default_factory=list)
    assignees: list[UserPayload] = Field(default_factory=list)
    milestone: MilestonePayload | None = None
    comments: int = 0
    created_at: datetime
    updated_at: datetime
    pull_request: dict[str, object] | None = None

    @property
    def is_pull_request(self) -> bool:
        return self.pull_request is not None

    @property
    def label_names(self) -> list[str]:
        return [label if isinstance(label, str) else label.name for label in self.labels]


class CommentPayload(Payload):
    id: int
    user: UserPayload | None = None
    body: str | None = None
    html_url: str
    created_at: datetime


class BranchRefPayload(Payload):
    ref: str
    sha: str


class PullPayload(Payload):
    number: int
    title: str
    state: str
    draft: bool = False
    user: UserPayload | None = None
    html_url: str
    created_at: datetime
    updated_at: datetime
    head: BranchRefPayload
    base: BranchRefPayload


class CheckRunPayload(Payload):
    status: str
    conclusion: str | None = None


class CheckRunsPayload(Payload):
    total_count: int
    check_runs: list[CheckRunPayload]


class CombinedStatusPayload(Payload):
    state: str
    total_count: int = 0


class WorkflowPayload(Payload):
    id: int
    name: str
    path: str
    state: str


class WorkflowsPayload(Payload):
    total_count: int
    workflows: list[WorkflowPayload]


class RunPayload(Payload):
    id: int
    name: str | None = None
    display_title: str | None = None
    workflow_id: int
    run_number: int
    run_attempt: int = 1
    event: str
    head_branch: str | None = None
    head_sha: str
    status: str | None = None
    conclusion: str | None = None
    html_url: str
    created_at: datetime
    updated_at: datetime
    run_started_at: datetime | None = None


class RunsPayload(Payload):
    total_count: int
    workflow_runs: list[RunPayload]


class StepPayload(Payload):
    number: int
    name: str
    status: str
    conclusion: str | None = None


class JobPayload(Payload):
    id: int
    name: str
    status: str
    conclusion: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    html_url: str | None = None
    steps: list[StepPayload] = Field(default_factory=list)


class JobsPayload(Payload):
    total_count: int
    jobs: list[JobPayload]


class ReleasePayload(Payload):
    id: int
    tag_name: str
    name: str | None = None
    draft: bool
    prerelease: bool
    html_url: str
    body: str | None = None
    created_at: datetime
    published_at: datetime | None = None


class TagCommitPayload(Payload):
    sha: str


class TagPayload(Payload):
    name: str
    commit: TagCommitPayload


class DeploymentPayload(Payload):
    id: int
    environment: str
    ref: str
    sha: str
    description: str | None = None
    creator: UserPayload | None = None
    created_at: datetime


class DeploymentStatusPayload(Payload):
    state: str
    environment_url: str | None = None


class EnvironmentPayload(Payload):
    name: str
    html_url: str | None = None


class EnvironmentsPayload(Payload):
    total_count: int
    environments: list[EnvironmentPayload]


# --- GraphQL: one query for open pull requests with their check rollup ---


class RollupPayload(Payload):
    state: Literal["EXPECTED", "ERROR", "FAILURE", "PENDING", "SUCCESS"]


class CommitPayload(Payload):
    status_check_rollup: RollupPayload | None = Field(None, alias="statusCheckRollup")


class CommitNodePayload(Payload):
    commit: CommitPayload


class CommitConnectionPayload(Payload):
    nodes: list[CommitNodePayload]


class AuthorPayload(Payload):
    login: str


class PullNodePayload(Payload):
    number: int
    title: str
    is_draft: bool = Field(alias="isDraft")
    url: str
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    head_ref_name: str = Field(alias="headRefName")
    head_ref_oid: str = Field(alias="headRefOid")
    base_ref_name: str = Field(alias="baseRefName")
    author: AuthorPayload | None = None
    commits: CommitConnectionPayload


class PullConnectionPayload(Payload):
    total_count: int = Field(alias="totalCount")
    nodes: list[PullNodePayload]


class PullRepositoryPayload(Payload):
    pull_requests: PullConnectionPayload = Field(alias="pullRequests")


class PullQueryPayload(Payload):
    repository: PullRepositoryPayload
