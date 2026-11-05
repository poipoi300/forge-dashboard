"""Turn GitHub payloads into the dashboard's facts, with no I/O.

The rules here decide what "CI is failing" and "checks are pending" mean.
Status and conclusion vocabularies are open-ended on GitHub's side, so every
rule classifies the values it knows and leaves the rest out of the verdict
rather than guessing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Literal

from forge_dashboard.domain.summary import CiFacts, IssueFacts
from forge_dashboard.github.payloads import (
    CheckRunPayload,
    CombinedStatusPayload,
    IssuePayload,
    MilestonePayload,
    RepoPayload,
    RollupPayload,
    RunPayload,
    WorkflowPayload,
)

ChecksState = Literal["passing", "failing", "pending", "none"]
RunVerdict = Literal["active", "failed", "cancelled", "passed", "ignored"]

TOP_LABELS = 3
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
_PASSED_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_ROLLUP_CHECKS: dict[str, ChecksState] = {
    "SUCCESS": "passing",
    "FAILURE": "failing",
    "ERROR": "failing",
    "PENDING": "pending",
    "EXPECTED": "pending",
}


def run_outcome(status: str | None, conclusion: str | None) -> str:
    """One word for a run, job, or step: its conclusion once finished, otherwise its status."""
    if status == "completed" and conclusion:
        return conclusion
    return status or "unknown"


def run_verdict(run: RunPayload) -> RunVerdict:
    if run.status != "completed":
        return "active"
    if run.conclusion in _FAILED_CONCLUSIONS:
        return "failed"
    if run.conclusion == "cancelled":
        return "cancelled"
    if run.conclusion in _PASSED_CONCLUSIONS:
        return "passed"
    return "ignored"


def run_started_at(run: RunPayload) -> float:
    return (run.run_started_at or run.created_at).timestamp()


def run_duration_s(run: RunPayload) -> float | None:
    """Wall-clock time of a finished run, from its start to its last update."""
    if run.status != "completed" or run.run_started_at is None:
        return None
    return max((run.updated_at - run.run_started_at).total_seconds(), 0.0)


def newest_run_per_workflow(runs: Iterable[RunPayload]) -> dict[int, RunPayload]:
    newest: dict[int, RunPayload] = {}
    for run in runs:
        current = newest.get(run.workflow_id)
        if current is None or (run.created_at, run.id) > (current.created_at, current.id):
            newest[run.workflow_id] = run
    return newest


def has_active_run(runs: Iterable[RunPayload]) -> bool:
    return any(run_verdict(run) == "active" for run in runs)


def ci_facts(
    workflows: Sequence[WorkflowPayload], latest_runs: Sequence[RunPayload], pr_checks: ChecksState
) -> CiFacts:
    """CI on the default branch from each active workflow's newest run.

    A failed run outranks a running one, so bad news is never hidden behind
    another workflow still going; a cancelled run (a superseded push, usually)
    only shows when nothing passed.
    """
    names = {workflow.id: workflow.name for workflow in workflows}
    runs = [run for run in latest_runs if run.workflow_id in names]
    verdicts = {run.id: run_verdict(run) for run in runs}
    seen = set(verdicts.values())
    conclusion: Literal["success", "failure", "cancelled", "in_progress", "none"]
    if "failed" in seen:
        conclusion = "failure"
    elif "active" in seen:
        conclusion = "in_progress"
    elif "passed" in seen:
        conclusion = "success"
    elif "cancelled" in seen:
        conclusion = "cancelled"
    else:
        conclusion = "none"
    newest = max(runs, key=lambda run: (run_started_at(run), run.id), default=None)
    return CiFacts(
        conclusion=conclusion,
        workflows=len(workflows),
        failing_workflows=sorted(names[run.workflow_id] for run in runs if verdicts[run.id] == "failed"),
        last_run_at=run_started_at(newest) if newest is not None else None,
        last_run_url=newest.html_url if newest is not None else None,
        open_pr_checks=pr_checks,
    )


def checks_from_check_runs(check_runs: Iterable[CheckRunPayload], status: CombinedStatusPayload) -> ChecksState:
    """A commit's checks from its check runs and its legacy combined commit status."""
    runs = list(check_runs)
    failed = {*_FAILED_CONCLUSIONS, "cancelled"}
    if any(run.conclusion in failed for run in runs) or status.state in ("failure", "error"):
        return "failing"
    has_status = status.total_count > 0
    if any(run.status != "completed" for run in runs) or (has_status and status.state == "pending"):
        return "pending"
    return "passing" if runs or has_status else "none"


def checks_from_rollup(rollup: RollupPayload | None) -> ChecksState:
    """A commit's checks from GraphQL's `statusCheckRollup` (None: the commit has no checks)."""
    return "none" if rollup is None else _ROLLUP_CHECKS[rollup.state]


def combine_checks(states: Iterable[ChecksState | None]) -> ChecksState:
    """The worst of several pull requests' checks; unfetched ones (None) do not count."""
    seen = {state for state in states if state is not None}
    for state in ("failing", "pending", "passing"):
        if state in seen:
            return state
    return "none"


def count_open_issues(
    repo: RepoPayload, listed: Sequence[IssuePayload], *, listing_complete: bool, open_prs: int
) -> int:
    """Open issues, not counting pull requests.

    GitHub's `open_issues_count` includes open pull requests. A listing that
    ended before its last page is the exact answer; otherwise the repository's
    own count, less the pull requests, is.
    """
    listed_issues = sum(1 for item in listed if not item.is_pull_request)
    if listing_complete:
        return listed_issues
    return max(repo.open_issues_count - open_prs, listed_issues)


def top_labels(issues: Iterable[IssuePayload], limit: int = TOP_LABELS) -> list[str]:
    counts = Counter(label for issue in issues for label in issue.label_names)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower(), item[0]))
    return [label for label, _ in ranked[:limit]]


def issue_facts(
    slug: str,
    repo: RepoPayload,
    *,
    open_issues: int,
    open_prs: int,
    listed: Sequence[IssuePayload],
    milestone: MilestonePayload | None,
) -> IssueFacts:
    issues = [item for item in listed if not item.is_pull_request]
    newest = max(issues, key=lambda issue: (issue.created_at, issue.number), default=None)
    return IssueFacts(
        github="ok",
        repo=slug,
        open_issues=open_issues,
        open_prs=open_prs,
        top_labels=top_labels(issues),
        newest_issue=newest.title if newest is not None else None,
        html_url=repo.html_url,
        milestone_open=milestone.open_issues if milestone is not None else None,
        milestone_closed=milestone.closed_issues if milestone is not None else None,
    )
