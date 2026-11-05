"""Local checks: JUnit and coverage parsing, runs, history, staleness, schedules, and gates."""

import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from conftest import MANIFEST, Git
from testing_support import wait_for

from forge_dashboard.core.events import EventEnvelope
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, ProjectRef
from forge_dashboard.domain.coverage import combined_coverage, parse_cobertura, parse_lcov
from forge_dashboard.domain.junit import JUnitError, parse_junit, read_junit
from forge_dashboard.domain.scheduling import IntervalSchedule
from forge_dashboard.services.checks import (
    CheckSettings,
    ChecksService,
    GateInput,
    HistoryInput,
    OnChangeSchedule,
    ReportInput,
    RunChecksInput,
    ScheduleInput,
    parse_gates,
    precommit_argv,
)
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService

PYTEST_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4" time="1.5">
<testcase classname="test.test_a" name="test_ok" time="0.1"/>
<testcase classname="test.test_a" name="test_fail" time="0.2">
<failure message="assert 1 == 2">trace</failure></testcase>
<testcase classname="test.test_a" name="test_skip" time="0"><skipped message="skip"/></testcase>
<testcase classname="test.test_b" name="test_ok2" time="0.3"/>
</testsuite></testsuites>"""

BUN_JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="bun test" tests="3" assertions="3" failures="1" skipped="0" time="0.05">
  <testsuite name="src/a.test.ts" tests="3" failures="1" skipped="0" time="0.04">
    <testsuite name="widget" tests="2" failures="1" skipped="0" time="0.02">
      <testcase name="renders" classname="widget" time="0.01"/>
      <testcase name="breaks" classname="widget" time="0.01"><failure type="AssertionError" message="nope"/></testcase>
    </testsuite>
    <testcase name="top level" classname="" time="0.01"><error message="boom">stack</error></testcase>
  </testsuite>
</testsuites>"""


def test_pytest_junit_counts_and_failures() -> None:
    report = parse_junit(PYTEST_JUNIT)
    assert (report.tests, report.failures, report.errors, report.skipped, report.passed) == (4, 1, 0, 1, 2)
    assert report.duration_s == 1.5
    [failure] = report.failed
    assert (failure.suite, failure.name, failure.message, failure.detail) == (
        "test.test_a",
        "test_fail",
        "assert 1 == 2",
        "trace",
    )


def test_bun_junit_nested_suites_are_counted_case_by_case() -> None:
    report = parse_junit(BUN_JUNIT)
    assert (report.tests, report.failures, report.errors) == (3, 1, 1)
    assert [case.suite for case in report.failed] == ["widget", "src/a.test.ts"]


def test_junit_rejects_non_reports(tmp_path: Path) -> None:
    with pytest.raises(JUnitError, match="not valid XML"):
        parse_junit("<oops")
    with pytest.raises(JUnitError, match="unexpected root"):
        parse_junit("<html/>")
    with pytest.raises(JUnitError, match="wrote no report"):
        read_junit(tmp_path / "missing.xml")


def test_coverage_reports_are_merged(tmp_path: Path) -> None:
    assert parse_cobertura('<coverage lines-covered="80" lines-valid="100" line-rate="0.8"/>') == (80, 100)
    assert parse_cobertura('<coverage line-rate="0.5"/>') == (5000, 10000)
    assert parse_lcov("SF:a\nLH:3\nLF:4\nend_of_record\nSF:b\nLH:1\nLF:4\n") == (4, 8)
    assert combined_coverage(tmp_path) is None
    (tmp_path / "coverage.xml").write_text('<coverage lines-covered="9" lines-valid="10"/>', encoding="utf-8")
    (tmp_path / "web" / "coverage").mkdir(parents=True)
    (tmp_path / "web" / "coverage" / "lcov.info").write_text("LH:1\nLF:10\n", encoding="utf-8")
    report = combined_coverage(tmp_path)
    assert report is not None and report.percent == 50.0 and report.format == "cobertura+lcov"


def test_gates_parse_ids_and_stages(forge_repo: Path, tmp_path: Path) -> None:
    gates = {gate.id: gate.stages for gate in parse_gates(forge_repo)}
    assert gates["ruff-check"] == ["pre-commit"] and gates["tests-and-diff-coverage"] == ["pre-push"]
    assert parse_gates(tmp_path) == []
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: [", encoding="utf-8")
    assert parse_gates(tmp_path) == []


def test_precommit_resolution_prefers_the_projects_own_environment(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pre-commit>=4"]\n', encoding="utf-8")
    assert precommit_argv(tmp_path) == ["uv", "run", "pre-commit"]
    assert precommit_argv(tmp_path / "elsewhere") in (["pre-commit"], [sys.executable, "-m", "pre_commit"])


FAKE_TESTS = """
import pathlib
import sys

passing = pathlib.Path("ok.flag").exists()
failing_case = "" if passing else '<testcase classname="t" name="b"><failure message="bad">x</failure></testcase>'
report = '<testsuites><testsuite><testcase classname="t" name="a"/>' + failing_case + "</testsuite></testsuites>"
pathlib.Path(sys.argv[1]).write_text(report, encoding="utf-8")
sys.exit(0 if passing else 1)
"""
PYTHON = sys.executable.replace(chr(92), "/")
CHECKS = f"""
[[checks]]
id = "unit"
label = "Unit tests"
argv = ["{PYTHON}", "scripts/fake_tests.py", "{{report_path}}"]
report = "junit"
stage = "push"

[[checks]]
id = "lint"
label = "Lint"
argv = ["{PYTHON}", "-c", "print('lint ok')"]
stage = "commit"
"""
PRECOMMIT = f"""repos:
  - repo: local
    hooks:
      - id: say-hi
        name: say hi
        entry: {PYTHON} -c "print('hi')"
        language: system
        pass_filenames: false
        always_run: true
"""


@pytest.fixture
async def checks_env(
    data_paths: DataPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[ChecksService, ProjectsService, DiscoveryService, JobsService, SchedulerService, Runtime]]:
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "pre-commit-home"))
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git, jobs, scheduler)
    runtime.register([jobs, scheduler, manifests, projects, git, checks, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")]})
    await runtime.start()
    try:
        yield checks, projects, discovery, jobs, scheduler, runtime
    finally:
        await runtime.stop()


def _manifest_with_checks(make_project: Callable[..., Path], name: str = "Alpha") -> Path:
    extra = {".pre-commit-config.yaml": PRECOMMIT, "scripts/fake_tests.py": FAKE_TESTS}
    return make_project(name, manifest=MANIFEST + CHECKS, extra=extra)


async def test_runs_record_results_publish_facts_and_detect_regressions(
    checks_env: tuple[ChecksService, ProjectsService, DiscoveryService, JobsService, SchedulerService, Runtime],
    make_project: Callable[..., Path],
    git: Git,
) -> None:
    checks, projects, discovery, jobs, _scheduler, runtime = checks_env
    alpha = _manifest_with_checks(make_project)
    (alpha / "ok.flag").write_text("", encoding="utf-8")
    git.commit_all(alpha, "Make tests pass")
    await discovery.add_project(PathInput(path=str(alpha)))
    await wait_for(lambda: checks.project_checks("alpha") is not None)

    first = await jobs.wait((await checks.run(RunChecksInput(project_id="alpha"))).job_id)
    assert first.status is JobStatus.SUCCEEDED and first.summary == "2 passed"
    facts = projects.summary("alpha").checks
    assert facts is not None and (facts.passed, facts.failed, facts.total, facts.tests_passed) == (2, 0, 2, 1)
    assert facts.result == "passing"
    assert not facts.stale and facts.sha == git(alpha, "rev-parse", "--short=7", "HEAD")

    (alpha / "ok.flag").unlink()
    git.commit_all(alpha, "Break tests")
    await projects.refresh("alpha")
    async with runtime.bus.subscribe() as events:
        second = await jobs.wait((await checks.run(RunChecksInput(project_id="alpha", check_id="unit"))).job_id)
        seen: list[EventEnvelope] = []
        while not events.empty():
            seen.append(events.get_nowait())
    assert second.status is JobStatus.FAILED and second.summary == "failing: Unit tests"
    regressed = [e for e in seen if e.type == "checks.regressed"]
    assert regressed and regressed[0].notify_kind == "check-regression"
    summary = projects.summary("alpha")
    assert summary.checks is not None and summary.checks.tests_failed == 1
    assert summary.checks.result == "failing"
    assert summary.health.level.value == "failing"

    history = await checks.history(HistoryInput(project_id="alpha", check_id="unit"))
    assert [p.outcome for p in history.points] == ["passed", "failed"] and history.pass_rate == 50
    latest = await checks.latest(ReportInput(project_id="alpha", check_id="unit"))
    assert latest.report is not None and latest.report.failed[0].name == "b"
    assert any("Unit tests: failed" in line for line in latest.log_tail)


async def test_head_runs_use_a_temporary_worktree_and_ignore_local_edits(
    checks_env: tuple[ChecksService, ProjectsService, DiscoveryService, JobsService, SchedulerService, Runtime],
    make_project: Callable[..., Path],
    git: Git,
    data_paths: DataPaths,
) -> None:
    checks, projects, discovery, jobs, _scheduler, _runtime = checks_env
    alpha = _manifest_with_checks(make_project)
    await discovery.add_project(PathInput(path=str(alpha)))
    await wait_for(lambda: checks.project_checks("alpha") is not None)
    (alpha / "ok.flag").write_text("", encoding="utf-8")  # uncommitted: invisible on HEAD
    final = await jobs.wait(
        (await checks.run(RunChecksInput(project_id="alpha", check_id="unit", on_head=True))).job_id
    )
    assert final.status is JobStatus.FAILED
    project = checks.project_checks("alpha")
    assert project is not None and project.checks[0].on_head
    assert git(alpha, "worktree", "list").count("\n") == 0
    assert not any((data_paths.worktrees / "alpha").glob("checks-*"))
    working_tree = await jobs.wait((await checks.run(RunChecksInput(project_id="alpha", check_id="unit"))).job_id)
    assert working_tree.status is JobStatus.SUCCEEDED
    summary = projects.summary("alpha").checks
    assert summary is not None and summary.passed == 1


async def test_schedules_and_staleness(
    checks_env: tuple[ChecksService, ProjectsService, DiscoveryService, JobsService, SchedulerService, Runtime],
    make_project: Callable[..., Path],
    git: Git,
) -> None:
    checks, projects, discovery, jobs, scheduler, _runtime = checks_env
    alpha = _manifest_with_checks(make_project)
    (alpha / "ok.flag").write_text("", encoding="utf-8")
    git.commit_all(alpha, "Pass")
    await discovery.add_project(PathInput(path=str(alpha)))
    await wait_for(lambda: checks.project_checks("alpha") is not None)
    await jobs.wait((await checks.run(RunChecksInput(project_id="alpha"))).job_id)
    (alpha / "more.txt").write_text("x", encoding="utf-8")
    git.commit_all(alpha, "Move HEAD")
    await projects.refresh("alpha")
    await wait_for(lambda: (facts := projects.summary("alpha").checks) is not None and facts.stale)

    interval = CheckSettings(schedule=IntervalSchedule(every_s=3600))
    await checks.set_settings(ScheduleInput(project_id="alpha", settings=interval))
    entry = scheduler.entry("checks:alpha")
    assert entry is not None and entry.target_action == "run"
    await checks.set_settings(ScheduleInput(project_id="alpha", settings=CheckSettings()))
    assert scheduler.entry("checks:alpha") is None

    on_change = CheckSettings(schedule=OnChangeSchedule(debounce_s=2))
    await checks.set_settings(ScheduleInput(project_id="alpha", settings=on_change))
    before = len(jobs.state.jobs)
    await checks.files_changed(projects.record("alpha"), frozenset({"worktree"}))
    await wait_for(lambda: len(jobs.state.jobs) > before, timeout_s=10)


async def test_gates_run_through_pre_commit(
    checks_env: tuple[ChecksService, ProjectsService, DiscoveryService, JobsService, SchedulerService, Runtime],
    make_project: Callable[..., Path],
) -> None:
    checks, _projects, discovery, jobs, _scheduler, _runtime = checks_env
    alpha = _manifest_with_checks(make_project)
    await discovery.add_project(PathInput(path=str(alpha)))
    await wait_for(lambda: checks.project_checks("alpha") is not None)
    project = checks.project_checks("alpha")
    assert project is not None and [gate.id for gate in project.gates] == ["say-hi"]
    gate = await jobs.wait((await checks.run_gate(GateInput(project_id="alpha", gate_id="say-hi"))).job_id)
    assert gate.status is JobStatus.SUCCEEDED
    commit_gates = await jobs.wait((await checks.run_commit_gates(ProjectRef(project_id="alpha"))).job_id)
    assert commit_gates.status is JobStatus.SUCCEEDED
    with pytest.raises(ActionError, match="no gate"):
        await checks.run_gate(GateInput(project_id="alpha", gate_id="ghost"))


async def test_projects_without_checks_are_refused(
    checks_env: tuple[ChecksService, ProjectsService, DiscoveryService, JobsService, SchedulerService, Runtime],
    make_project: Callable[..., Path],
) -> None:
    checks, _projects, discovery, _jobs, _scheduler, _runtime = checks_env
    await discovery.add_project(PathInput(path=str(make_project("Plain"))))
    with pytest.raises(ActionError, match="declares no checks"):
        await checks.run(RunChecksInput(project_id="plain"))
    with pytest.raises(ActionError, match=r"no \.pre-commit-config\.yaml"):
        await checks.run_push_gate(ProjectRef(project_id="plain"))
