"""Adapts the GitHub service to what other services ask of GitHub, so they never import it."""

from __future__ import annotations

from typing import Protocol

from forge_dashboard.github.models import (
    CreatePullRequestInput,
    GitHubIssueDetail,
    GitHubPull,
    GitHubRepoInfo,
    IssueRef,
)
from forge_dashboard.services.agents import IssueBrief


class GitHubCalls(Protocol):
    """The part of `GitHubService` the bridge uses."""

    async def issue(self, request: IssueRef) -> GitHubIssueDetail: ...

    async def create_pull_request(self, request: CreatePullRequestInput) -> GitHubPull: ...

    def repo_info(self, project_id: str) -> GitHubRepoInfo | None: ...


class GitHubBridge:
    """Implements the agents service's `IssueSource` and `PullRequestOpener`, and the forks parent lookup."""

    def __init__(self, github: GitHubCalls) -> None:
        self._github = github

    async def issue_brief(self, project_id: str, number: int) -> IssueBrief:
        detail = await self._github.issue(IssueRef(project_id=project_id, number=number))
        return IssueBrief(number=number, title=detail.issue.title, body=detail.body, url=detail.issue.html_url)

    async def open_pull_request(self, project_id: str, head: str, base: str, title: str, body: str) -> str:
        pull = await self._github.create_pull_request(
            CreatePullRequestInput(project_id=project_id, head=head, base=base, title=title, body=body)
        )
        return pull.html_url

    async def parent_clone_url(self, project_id: str) -> str | None:
        """The clone URL of the repository a GitHub fork was forked from."""
        info = self._github.repo_info(project_id)
        if info is None or not info.fork or info.parent is None:
            return None
        return f"https://github.com/{info.parent}.git"
