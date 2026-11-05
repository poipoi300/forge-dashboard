"""Read one repository's data through the client.

`take_snapshot` gathers what the overview needs and is what the poll loop
runs; the other functions back the on-demand queries. Requests run one after
another: they are conditional and mostly answered with a 304, and sequential
reads keep the rate-limit gate and failures easy to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from forge_dashboard.github.client import GitHubClient
from forge_dashboard.github.facts import (
    ChecksState,
    checks_from_check_runs,
    checks_from_rollup,
    combine_checks,
    count_open_issues,
    newest_run_per_workflow,
)
from forge_dashboard.github.models import GitHubPull
from forge_dashboard.github.payloads import (
    CheckRunsPayload,
    CombinedStatusPayload,
    IssuePayload,
    MilestonePayload,
    PullPayload,
    PullQueryPayload,
    RepoPayload,
    RunPayload,
    RunsPayload,
    WorkflowPayload,
    WorkflowsPayload,
)

PullState = Literal["open", "closed", "all"]

# Open pull requests listed per poll, and how many REST pages are counted for the total.
PULL_SAMPLE = 30
PULL_PAGES = 3
# Pages of open issues (pull requests included) sampled for labels and the newest title.
ISSUE_SAMPLE_PAGES = 2
_GRAPHQL_PULL_STATES: dict[PullState, list[JsonValue]] = {
    "open": ["OPEN"],
    "closed": ["CLOSED", "MERGED"],
    "all": ["OPEN", "CLOSED", "MERGED"],
}

PULLS_QUERY = """
query($owner: String!, $name: String!, $states: [PullRequestState!], $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: $states, first: $first, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes {
        number
        title
        isDraft
        url
        createdAt
        updatedAt
        headRefName
        headRefOid
        baseRefName
        author { login }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class PullListing:
    """Pull requests, most recently updated first.

    `total` is exact over GraphQL, and counts at most `PULL_PAGES` pages over REST.
    """

    pulls: list[GitHubPull]
    total: int


@dataclass(frozen=True)
class RepoSnapshot:
    """Everything the overview shows for one repository, as of one poll."""

    repo: RepoPayload
    open_issues: int
    open_prs: int
    pulls: list[GitHubPull]
    listed_issues: list[IssuePayload]
    workflows: list[WorkflowPayload]
    latest_runs: list[RunPayload]
    milestone: MilestonePayload | None


async def take_snapshot(
    client: GitHubClient, slug: str, *, milestone_title: str | None, pull_check_limit: int
) -> RepoSnapshot:
    """Poll one repository: metadata, pull requests with checks, issues, workflows and their newest runs.

    Raises:
        GitHubError: when any request fails; nothing partial is returned.
    """
    repo = await client.get_model(f"/repos/{slug}", RepoPayload)
    pulls = await fetch_pulls(client, slug, "open", limit=PULL_SAMPLE, check_limit=pull_check_limit)
    issues = await client.get_pages(
        f"/repos/{slug}/issues",
        IssuePayload,
        {"state": "open", "sort": "created", "direction": "desc"},
        max_pages=ISSUE_SAMPLE_PAGES,
    )
    workflows = await fetch_active_workflows(client, slug)
    latest_runs = await fetch_latest_runs(client, slug, repo.default_branch, workflows)
    milestone = await find_milestone(client, slug, milestone_title) if milestone_title is not None else None
    return RepoSnapshot(
        repo=repo,
        open_issues=count_open_issues(
            repo, issues.items, listing_complete=not issues.more_available, open_prs=pulls.total
        ),
        open_prs=pulls.total,
        pulls=pulls.pulls,
        listed_issues=issues.items,
        workflows=workflows,
        latest_runs=latest_runs,
        milestone=milestone,
    )


def pull_checks_rollup(pulls: list[GitHubPull]) -> ChecksState:
    return combine_checks(pull.checks for pull in pulls)


async def fetch_pulls(
    client: GitHubClient,
    slug: str,
    state: PullState,
    *,
    limit: int,
    check_limit: int,
    essential: bool = False,
) -> PullListing:
    """Pull requests with their check status.

    One GraphQL query covers every listed pull request when authenticated.
    Over REST each pull request costs two more requests (check runs and
    commit status), so only the first `check_limit` get a status.
    """
    if client.auth.authenticated:
        owner, name = slug.split("/", 1)
        data = await client.graphql(
            PULLS_QUERY,
            {"owner": owner, "name": name, "states": _GRAPHQL_PULL_STATES[state], "first": limit},
            PullQueryPayload,
            essential=essential,
        )
        connection = data.repository.pull_requests
        return PullListing(
            pulls=[
                GitHubPull.from_node(
                    node,
                    checks_from_rollup(
                        node.commits.nodes[0].commit.status_check_rollup if node.commits.nodes else None
                    ),
                )
                for node in connection.nodes
            ],
            total=connection.total_count,
        )
    page = await client.get_pages(
        f"/repos/{slug}/pulls",
        PullPayload,
        {"state": state, "sort": "updated", "direction": "desc"},
        max_pages=PULL_PAGES,
        essential=essential,
    )
    pulls: list[GitHubPull] = []
    for index, pull in enumerate(page.items[:limit]):
        checks = await _commit_checks(client, slug, pull.head.sha, essential) if index < check_limit else None
        pulls.append(GitHubPull.from_payload(pull, checks))
    return PullListing(pulls=pulls, total=len(page.items))


async def _commit_checks(client: GitHubClient, slug: str, sha: str, essential: bool) -> ChecksState:
    check_runs = await client.get_model(
        f"/repos/{slug}/commits/{sha}/check-runs", CheckRunsPayload, {"per_page": 100}, essential=essential
    )
    status = await client.get_model(f"/repos/{slug}/commits/{sha}/status", CombinedStatusPayload, essential=essential)
    return checks_from_check_runs(check_runs.check_runs, status)


async def fetch_active_workflows(client: GitHubClient, slug: str, *, essential: bool = False) -> list[WorkflowPayload]:
    """The repository's active workflows (the first 100; a disabled or deleted one has no say in CI health)."""
    payload = await client.get_model(
        f"/repos/{slug}/actions/workflows", WorkflowsPayload, {"per_page": 100}, essential=essential
    )
    return [workflow for workflow in payload.workflows if workflow.state == "active"]


async def fetch_latest_runs(
    client: GitHubClient, slug: str, branch: str, workflows: list[WorkflowPayload], *, essential: bool = False
) -> list[RunPayload]:
    """The newest run of each given workflow on `branch`, ordered by workflow id.

    One request lists the branch's latest 100 runs. A workflow missing from
    them (quiet for a long time) is asked for individually, but only when the
    branch has more runs than that listing held.
    """
    window: dict[str, str | int | bool] = {"branch": branch, "exclude_pull_requests": True}
    listing = await client.get_model(
        f"/repos/{slug}/actions/runs", RunsPayload, {**window, "per_page": 100}, essential=essential
    )
    wanted = {workflow.id for workflow in workflows}
    newest = newest_run_per_workflow(run for run in listing.workflow_runs if run.workflow_id in wanted)
    if listing.total_count > len(listing.workflow_runs):
        for workflow in workflows:
            if workflow.id in newest:
                continue
            own = await client.get_model(
                f"/repos/{slug}/actions/workflows/{workflow.id}/runs",
                RunsPayload,
                {**window, "per_page": 1},
                essential=essential,
            )
            newest.update(newest_run_per_workflow(own.workflow_runs))
    return sorted(newest.values(), key=lambda run: run.workflow_id)


async def find_milestone(
    client: GitHubClient, slug: str, title: str, *, essential: bool = False
) -> MilestonePayload | None:
    """The milestone titled exactly `title`, open or closed."""
    page = await client.get_pages(
        f"/repos/{slug}/milestones", MilestonePayload, {"state": "all"}, max_pages=2, essential=essential
    )
    return next((milestone for milestone in page.items if milestone.title == title), None)
