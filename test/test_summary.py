"""Pure rules behind the overview: health rollup, V1 progress, last change, attention, report."""

from datetime import date

from forge_dashboard.domain import summary as rules
from forge_dashboard.domain.report import render_markdown
from forge_dashboard.domain.summary import (
    CheckFacts,
    CiFacts,
    ForkFacts,
    GitFacts,
    HealthLevel,
    Lifecycle,
    ManifestFacts,
    ProjectFacts,
    ProjectIdentity,
    ServiceBrief,
    ServicesFacts,
    StandardsFacts,
    UserProjectState,
)

IDENTITY = ProjectIdentity(id="demo", name="Demo", path="C:/demo", role="managed", machine="box")


def _facts(**sources: object) -> ProjectFacts:
    return ProjectFacts(identity=IDENTITY).model_copy(update=sources)


def test_no_signals_means_unknown_health() -> None:
    assert rules.compose_summary(_facts()).health.level is HealthLevel.UNKNOWN


def test_passing_checks_and_ci_are_healthy() -> None:
    facts = _facts(checks=CheckFacts(passed=3, total=3), ci=CiFacts(conclusion="success"))
    summary = rules.compose_summary(facts)
    assert summary.health.level is HealthLevel.GOOD
    assert {reason.source for reason in summary.health.reasons} == {"checks", "ci"}
    assert summary.attention == []


def test_worst_signal_wins_and_every_reason_is_explained() -> None:
    facts = _facts(
        checks=CheckFacts(passed=2, total=2, stale=True),
        ci=CiFacts(conclusion="failure", failing_workflows=["CI"]),
        standards=StandardsFacts(passing=10, total=13),
    )
    summary = rules.compose_summary(facts)
    assert summary.health.level is HealthLevel.FAILING
    messages = [reason.message for reason in summary.health.reasons]
    assert "CI failing: CI" in messages
    assert "local checks passed but are stale against HEAD" in messages
    assert "3 standards checks failing" in messages
    assert [item.level for item in summary.attention] == ["bad", "warn", "warn"]


def test_invalid_manifest_and_crashed_services_fail_health() -> None:
    crashed = ServiceBrief(id="app", label="App", state="failed", machine="box")
    facts = _facts(manifest=ManifestFacts(valid=False, error_count=1), services=ServicesFacts(services=[crashed]))
    reasons = rules.health_reasons(facts)
    assert [(r.source, r.level) for r in reasons] == [
        ("manifest", HealthLevel.FAILING),
        ("services", HealthLevel.FAILING),
    ]
    assert reasons[0].message == "manifest invalid (1 error)"


def test_weighted_v1_progress_names_the_next_milestone() -> None:
    progress = rules.v1_progress(
        [("Scaffold", 1, True), ("Core", 3, False), ("Polish", 1, False)], date(2026, 12, 1), date(2026, 11, 5)
    )
    assert (progress.done, progress.total, progress.percent) == (1, 5, 20.0)
    assert progress.next_milestone == "Core"
    assert progress.days_left == 26
    assert progress.caption == "1/3 · 26d left"
    assert progress.detail == "next: Core"


def test_overdue_and_github_milestone_progress() -> None:
    overdue = rules.v1_progress([("Only", 1, True)], date(2026, 11, 1), date(2026, 11, 5))
    assert overdue.caption == "1/1 · 4d overdue" and overdue.next_milestone is None
    assert overdue.detail == ""
    github = rules.github_milestone_progress(3, 1, None, date(2026, 11, 5))
    assert (github.percent, github.caption) == (25.0, "1/4")
    assert rules.v1_progress([], None, date(2026, 11, 5)).percent == 0


def test_last_change_is_the_later_of_commit_and_working_tree() -> None:
    assert rules.last_change(GitFacts(last_commit_at=100, last_worktree_change_at=200)).source == "working-tree"
    assert rules.last_change(GitFacts(last_commit_at=300, last_worktree_change_at=200)).at == 300
    assert rules.last_change(GitFacts(last_commit_at=300)).source == "commit"
    assert rules.last_change(GitFacts()).source == "unknown"
    assert rules.last_change(None).at is None


def test_fork_and_status_attention_items() -> None:
    facts = _facts(
        fork=ForkFacts(is_fork=True, last_sync="needs-attention"),
        user=UserProjectState(status=Lifecycle.BLOCKED, status_note="waiting on API keys"),
    )
    summary = rules.compose_summary(facts)
    assert [(item.source, item.message) for item in summary.attention] == [
        ("fork", "fork sync needs-attention"),
        ("status", "waiting on API keys"),
    ]
    assert summary.fork is not None
    assert rules.compose_summary(_facts(fork=ForkFacts(is_fork=False))).fork is None


def test_report_lists_projects_and_attention() -> None:
    failing = rules.compose_summary(_facts(ci=CiFacts(conclusion="failure"), git=GitFacts(last_commit_at=1000)))
    report = render_markdown([failing], now=1000 + 7200)
    assert (
        "| Demo | active | failing | 2h ago (commit) | no manifest | none run | failure | n/a | `C:/demo` |" in report
    )
    assert "- **Demo**: CI failing: a workflow" in report
    assert "Nothing needs attention." in render_markdown([], now=0)
