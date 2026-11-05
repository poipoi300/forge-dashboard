"""The bridge turns GitHub service calls into what agents and forks ask for."""

from forge_dashboard.github.bridge import GitHubBridge
from forge_dashboard.github.models import (
    CreatePullRequestInput,
    GitHubIssue,
    GitHubIssueDetail,
    GitHubPull,
    GitHubRepoInfo,
    IssueRef,
)


class StubGitHub:
    def __init__(self, info: GitHubRepoInfo | None) -> None:
        self.info = info
        self.created: list[CreatePullRequestInput] = []

    async def issue(self, request: IssueRef) -> GitHubIssueDetail:
        issue = GitHubIssue(
            number=request.number,
            title="Fix it",
            state="open",
            created_at=0,
            updated_at=0,
            html_url="https://github.com/o/r/issues/5",
        )
        return GitHubIssueDetail(issue=issue, body="Details", comments=[])

    async def create_pull_request(self, request: CreatePullRequestInput) -> GitHubPull:
        self.created.append(request)
        return GitHubPull(
            number=9,
            title=request.title,
            head_branch=request.head,
            base_branch=request.base or "main",
            head_sha="abc",
            created_at=0,
            updated_at=0,
            html_url="https://github.com/o/r/pull/9",
        )

    def repo_info(self, project_id: str) -> GitHubRepoInfo | None:
        return self.info


def _info(fork: bool, parent: str | None) -> GitHubRepoInfo:
    return GitHubRepoInfo(
        repo="me/r", html_url="https://github.com/me/r", default_branch="main", private=False, fork=fork, parent=parent
    )


async def test_issue_briefs_and_pull_requests_come_from_the_github_service() -> None:
    stub = StubGitHub(None)
    bridge = GitHubBridge(stub)
    brief = await bridge.issue_brief("p", 5)
    assert (brief.number, brief.title, brief.body, brief.url) == (
        5,
        "Fix it",
        "Details",
        "https://github.com/o/r/issues/5",
    )
    url = await bridge.open_pull_request("p", "topic", "main", "Title", "Body")
    assert url == "https://github.com/o/r/pull/9"
    assert (stub.created[0].head, stub.created[0].base, stub.created[0].body) == ("topic", "main", "Body")


async def test_parent_clone_urls_only_for_github_forks() -> None:
    assert await GitHubBridge(StubGitHub(None)).parent_clone_url("p") is None
    assert await GitHubBridge(StubGitHub(_info(False, None))).parent_clone_url("p") is None
    fork = GitHubBridge(StubGitHub(_info(True, "octocat/Hello-World")))
    assert await fork.parent_clone_url("p") == "https://github.com/octocat/Hello-World.git"
