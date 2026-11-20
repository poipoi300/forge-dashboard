"""Standards: compliance against a live (fixture) forge, bring-up, adopt, and the template revision."""

import os
import shutil
import tomllib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FORGE_PYPROJECT, FORGE_URL, MANIFEST, Git, write_files, write_hook
from testing_support import fake_harness_config, wait_for

from forge_dashboard.branchwork import BranchRunState
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, ProjectRef
from forge_dashboard.domain import standards as rules
from forge_dashboard.domain.harness import HarnessConfig
from forge_dashboard.domain.standards import ForgeSource
from forge_dashboard.services.agents import AgentsConfig, AgentsService, BranchRunInput, BranchRunsInput
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService
from forge_dashboard.services.standards import StandardsService, set_template_revision, standards_brief

TRAVELLING = ["A note from the maintainer", "Verifying", "Commits", "Taste"]


@pytest.fixture
def forge_repo(forge_repo: Path, git: Git) -> Path:
    """The fixture forge, published on GitHub so projects can reference it."""
    git(forge_repo, "remote", "add", "origin", f"{FORGE_URL}.git")
    return forge_repo


def _results(report: rules.ComplianceReport) -> dict[str, str]:
    return {check.id: check.result for check in report.checks}


def test_a_bare_repo_fails_almost_everything(forge_repo: Path, tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    report = rules.evaluate(bare, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    results = _results(report)
    assert results["manifest"] == "fail" and results["language-hooks"] == "n/a" and results["python-strict"] == "n/a"
    assert report.passing == 0 and report.total == 11
    assert {check.fix for check in report.checks if check.id in ("agents-sections", "claude-md", "gitignore")} == {
        "deterministic"
    }


def _links(revision: str) -> str:
    base = f"{FORGE_URL}/blob/{revision}/AGENTS.md"
    return (
        "## Working rules\n\n"
        f"- [A note from the maintainer]({base}#a-note-from-the-maintainer)\n"
        f"- [Verifying]({base}#verifying)\n- [Commits]({base}#commits)\n- [Taste]({base}#taste)\n"
    )


def _referencing_project(
    tmp_path: Path, git: Git, *, hooks_rev: str, ci_rev: str, links_rev: str | None = None
) -> Path:
    project = tmp_path / "referencing"
    write_files(
        project,
        {
            "forge.project.toml": MANIFEST.replace("{name}", "Referencing").replace("{project_id}", "referencing"),
            "AGENTS.md": f"# Referencing\n\nIntro.\n\n{_links(links_rev or hooks_rev)}",
            "CLAUDE.md": "@AGENTS.md\n",
            "README.md": "# Referencing\n\n## Setup\n\nRun it.\n",
            ".gitignore": "__pycache__/\n.venv/\n.coverage\n",
            "pyproject.toml": FORGE_PYPROJECT.replace('name = "forge"', 'name = "referencing"'),
            ".pre-commit-config.yaml": f"""
                default_stages: [pre-commit]
                repos:
                  - repo: {FORGE_URL}
                    rev: {hooks_rev}
                    hooks:
                      - id: trailing-whitespace
                      - id: end-of-file-fixer
                      - id: check-merge-conflict
                      - id: check-toml
                      - id: tests-and-diff-coverage
                        args: [--python]
                  - repo: local
                    hooks:
                      - id: ruff-format
                        name: ruff format
                        entry: uv run ruff format
                        language: system
                      - id: ruff-check
                        name: ruff check
                        entry: uv run ruff check
                        language: system
                      - id: mypy
                        name: mypy
                        entry: uv run mypy .
                        language: system
                """,
            ".github/workflows/ci.yml": f"""
                on: [push, pull_request]
                jobs:
                  gates:
                    uses: owner/forge/.github/workflows/gates.yml@{ci_rev}
                    with:
                      os: '["ubuntu-24.04"]'
                """,
            "test/test_gates.py": "# reads .pre-commit-config.yaml\n",
        },
    )
    git.init(project)
    git.commit_all(project, "Init")
    for kind in ("pre-commit", "pre-push"):
        write_hook(project / ".git" / "hooks" / kind, "#!/bin/sh\n# File generated by pre-commit\n")
    return project


def test_a_project_referencing_forge_at_one_pinned_revision_passes(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    revision = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=revision, ci_rev=revision)
    report = rules.evaluate(project, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    assert {k: v for k, v in _results(report).items() if v != "pass"} == {}
    assert report.passing == report.total == 13


def test_copies_of_forge_fail_every_reference_check(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    project = tmp_path / "copied"
    shutil.copytree(forge_repo, project, ignore=shutil.ignore_patterns(".git"))
    write_files(project, {"forge.project.toml": MANIFEST.replace("{name}", "Copied").replace("{project_id}", "copied")})
    git.init(project)
    git.commit_all(project, "Init")
    report = rules.evaluate(project, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    details = {check.id: check.detail for check in report.checks if check.result == "fail"}
    assert {"agents-sections", "forge-hooks", "base-hooks", "ci"} <= set(details)
    assert (
        "copies forge's sections: A note from the maintainer, Verifying, Commits, Taste" in details["agents-sections"]
    )
    assert details["forge-hooks"] == f"no {FORGE_URL} repo in .pre-commit-config.yaml"
    assert details["ci"] == "no job uses owner/forge/.github/workflows/gates.yml"


@pytest.mark.parametrize(
    ("hooks_rev", "ci_rev", "failing"),
    [
        ("main", "HEAD", {"forge-hooks": "rev main is not a commit SHA", "ci": "pins differ: hooks main, CI HEAD"}),
        ("HEAD", "v1", {"ci": "gates.yml@v1 is not a commit SHA"}),
        ("HEAD", "0" * 40, {"ci": "pins differ: hooks HEAD, CI 0000000000000000000000000000000000000000"}),
    ],
)
def test_floating_or_disagreeing_pins_fail(
    forge_repo: Path, tmp_path: Path, git: Git, hooks_rev: str, ci_rev: str, failing: dict[str, str]
) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(
        tmp_path, git, hooks_rev=hooks_rev.replace("HEAD", head), ci_rev=ci_rev.replace("HEAD", head), links_rev=head
    )
    report = rules.evaluate(project, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    found = {check.id: check.detail for check in report.checks if check.result == "fail"}
    assert found == {key: value.replace("HEAD", head) for key, value in failing.items()}


def test_rule_links_replace_copied_sections_and_follow_the_revision(forge_repo: Path) -> None:
    forge_agents = (forge_repo / "AGENTS.md").read_text(encoding="utf-8")
    copied = (
        "# Mine\n\n## Intro\n\nHi.\n\n" + rules.sections(forge_agents)["Verifying"] + "\n## Project notes\n\nOurs.\n"
    )
    linked = rules.apply_rule_links(copied, "owner/forge", "a" * 40, TRAVELLING)
    assert "Hi." in linked and "Ours." in linked and "A task is done when the gates pass." not in linked
    assert linked.endswith(_links("a" * 40))
    moved = rules.apply_rule_links(linked, "owner/forge", "b" * 40, TRAVELLING)
    assert moved.endswith(_links("b" * 40)) and moved.count("## Working rules") == 1 and "Ours." in moved


def test_template_revision_is_written_without_touching_the_rest() -> None:
    manifest = MANIFEST.replace("{name}", "X").replace("{project_id}", "x")
    updated = set_template_revision(manifest, "abc123", datetime(2026, 11, 5, 12, tzinfo=UTC))
    assert updated.startswith(manifest.rstrip("\n").lstrip("\n")[:20]) or manifest.strip() in updated
    data = tomllib.loads(updated)
    assert data["forge"]["template_revision"] == "abc123"
    again = set_template_revision(updated, "def456", datetime(2026, 11, 6, tzinfo=UTC))
    assert tomllib.loads(again)["forge"]["template_revision"] == "def456" and again.count("[forge]") == 1


def test_the_brief_carries_failing_checks_rules_and_forge_text(forge_repo: Path, tmp_path: Path) -> None:
    report = rules.evaluate(tmp_path, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    brief = standards_brief(
        project_name="Demo",
        languages=["python"],
        failing=report.failing,
        forge=ForgeSource(forge_repo),
        travelling=TRAVELLING,
        revision="abc",
    )
    assert brief.startswith("# Bring up to standards")
    assert "<<<FORGE_AGENTS_MD" in brief and "Guard clauses first." in brief
    assert 'template_revision = "abc"' in brief and "- forge-hooks:" in brief
    assert "owner/forge/.github/workflows/gates.yml@abc" in brief and "<<<FORGE_HOOKS" in brief


NONCOMPLIANT = {
    "README.md": "# Messy\n\nNo instructions.\n",
    ".gitignore": "__pycache__/\n",
    "app.py": "print('hi')\n",
}


@pytest.fixture
async def standards_env(
    data_paths: DataPaths, tmp_path: Path, forge_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService]]:
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "pre-commit-home"))
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git_service = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git_service, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    standards = StandardsService(runtime, projects, jobs, checks, agents)
    runtime.register([jobs, scheduler, manifests, projects, git_service, checks, agents, standards, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")], "forge_repo": str(forge_repo)})
    await runtime.db.kv_put("config", "agents", AgentsConfig(harness=fake_harness_config()).model_dump(mode="json"))
    await runtime.start()
    try:
        yield standards, projects, discovery, jobs, agents
    finally:
        await runtime.stop()


async def test_bring_up_fixes_deterministically_then_with_the_agent_on_a_branch(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    make_project: Callable[..., Path],
    git: Git,
    forge_repo: Path,
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    messy = make_project("Messy", extra=NONCOMPLIANT)
    await discovery.add_project(PathInput(path=str(messy)))
    await wait_for(lambda: (facts := projects.summary("messy").standards) is not None and facts.total > 0)
    before = projects.summary("messy").standards
    assert before is not None and before.passing < before.total and before.drift

    final = await jobs.wait((await standards.bring_up(ProjectRef(project_id="messy"))).job_id)
    assert final.status is JobStatus.ATTENTION and final.summary is not None and "->" in final.summary
    [run] = (await agents.branch_runs(BranchRunsInput(project_id="messy", kind="standards"))).runs
    assert run.state is BranchRunState.READY and run.branch.startswith("forge-standards/")
    assert run.hooks_installed is True
    for kind in ("pre-commit", "pre-push"):
        hook = messy / ".git" / "hooks" / kind
        assert hook.is_file(), "hooks are installed in the main repo"
        assert os.access(hook, os.X_OK), "installed hooks must be executable"
    assert not (messy / "CLAUDE.md").exists(), "the user's working tree is untouched until merge"

    worktree = Path(run.worktree)
    after = rules.evaluate(worktree, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="x")
    passing = {check.id for check in after.checks if check.result == "pass"}
    assert {"manifest", "agents-md", "agents-sections", "claude-md", "readme", "gitignore"} <= passing
    assert not (worktree / "tools").exists(), "bring-up references forge instead of copying it"
    head = git(forge_repo, "rev-parse", "HEAD")
    assert after.template_revision == head

    await agents.merge_branch(BranchRunInput(run_id=run.id))
    assert (messy / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    standing = await standards.evaluate(ProjectRef(project_id="messy"))
    assert standing.report is not None and standing.behind == 0 and standing.report.template_revision == head
    facts = projects.summary("messy").standards
    assert facts is not None and facts.passing > before.passing and facts.forge_commits_behind == 0


async def test_forge_commits_since_the_revision_are_listed(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    make_project: Callable[..., Path],
    git: Git,
    forge_repo: Path,
) -> None:
    standards, _projects, discovery, _jobs, _agents = standards_env
    revision = git(forge_repo, "rev-parse", "HEAD")
    manifest = MANIFEST.replace("{name}", "Aligned").replace("{project_id}", "aligned")
    manifest += f'\n[forge]\ntemplate_revision = "{revision}"\n'
    aligned = make_project("Aligned", manifest=manifest)
    (forge_repo / "NEWS.md").write_text("news\n", encoding="utf-8")
    git.commit_all(forge_repo, "Tighten a gate")
    await discovery.add_project(PathInput(path=str(aligned)))
    standing = await standards.evaluate(ProjectRef(project_id="aligned"))
    assert standing.behind == 1 and [c.subject for c in standing.forge_commits_since] == ["Tighten a gate"]
    report = await standards.report(ProjectRef(project_id="aligned"))
    assert report.report is not None and report.report.forge_head == git(forge_repo, "rev-parse", "HEAD")


async def test_adopt_writes_a_valid_manifest_with_a_repair_loop(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    make_project: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    loose = make_project("Loose", manifest=None)
    await discovery.add_project(PathInput(path=str(loose)))
    project_id = next(record.id for record in projects.records() if record.path == loose.resolve())
    monkeypatch.setenv("FAKE_HARNESS_SABOTAGE_FIRST", "1")
    final = await jobs.wait((await standards.adopt(ProjectRef(project_id=project_id))).job_id)
    assert final.status is JobStatus.ATTENTION
    [run] = (await agents.branch_runs(BranchRunsInput(project_id=project_id, kind="adopt"))).runs
    assert run.agent_attempts == 2 and run.state is BranchRunState.READY
    await agents.merge_branch(BranchRunInput(run_id=run.id))
    assert (loose / "forge.project.toml").is_file()
    await projects.refresh(project_id)
    with pytest.raises(ActionError, match="already has"):
        await standards.adopt(ProjectRef(project_id=project_id))


StandardsEnv = tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService]


def _id_of(projects: ProjectsService, path: Path) -> str:
    return next(record.id for record in projects.records() if record.path == path.resolve())


async def test_adopt_stops_at_a_harness_that_did_not_run_instead_of_retrying_it(
    standards_env: StandardsEnv, make_project: Callable[..., Path]
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    loose = make_project("Loose", manifest=None)
    await discovery.add_project(PathInput(path=str(loose)))
    project_id = _id_of(projects, loose)
    await agents.update_config(AgentsConfig(harness=HarnessConfig(command=["definitely-not-a-harness-xyz"])))

    final = await jobs.wait((await standards.adopt(ProjectRef(project_id=project_id))).job_id)

    assert final.status is JobStatus.FAILED and final.summary == "agent harness-missing"
    [run] = (await agents.branch_runs(BranchRunsInput(project_id=project_id, kind="adopt"))).runs
    assert run.state is BranchRunState.FAILED and run.error == "harness-missing" and run.agent_attempts == 1
    assert len(run.agent_run_ids) == 1


async def test_a_failure_while_adopting_fails_the_run_instead_of_leaving_it_running(
    standards_env: StandardsEnv, make_project: Callable[..., Path]
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    loose = make_project("Loose", manifest=None)
    await discovery.add_project(PathInput(path=str(loose)))
    project_id = _id_of(projects, loose)
    hook = loose / ".git" / "hooks" / "pre-commit"
    write_hook(hook, "#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")

    final = await jobs.wait((await standards.adopt(ProjectRef(project_id=project_id))).job_id)

    assert final.status is JobStatus.FAILED and "hook says no" in (final.error or "")
    [run] = (await agents.branch_runs(BranchRunsInput(project_id=project_id, kind="adopt"))).runs
    assert run.state is BranchRunState.FAILED and "hook says no" in (run.error or "")
    discarded = await agents.discard_branch(BranchRunInput(run_id=run.id))
    assert discarded.state is BranchRunState.DISCARDED and not Path(run.worktree).exists()


async def test_adopt_starts_from_the_repos_own_default_branch(
    standards_env: StandardsEnv, make_project: Callable[..., Path], git: Git
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    loose = make_project("Loose", manifest=None)
    git(loose, "branch", "-m", "main", "trunk")
    await discovery.add_project(PathInput(path=str(loose)))
    project_id = _id_of(projects, loose)

    final = await jobs.wait((await standards.adopt(ProjectRef(project_id=project_id))).job_id)

    assert final.status is JobStatus.ATTENTION
    [run] = (await agents.branch_runs(BranchRunsInput(project_id=project_id, kind="adopt"))).runs
    assert run.base_branch == "trunk" and run.state is BranchRunState.READY


async def test_without_the_forge_repo_bring_up_is_refused(
    data_paths: DataPaths, tmp_path: Path, make_project: Callable[..., Path]
) -> None:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git_service = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git_service, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    standards = StandardsService(runtime, projects, jobs, checks, agents)
    runtime.register([jobs, scheduler, manifests, projects, git_service, checks, agents, standards, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")]})
    await runtime.start()
    try:
        await discovery.add_project(PathInput(path=str(make_project("Alone"))))
        with pytest.raises(ActionError, match="forge repo was not found"):
            await standards.bring_up(ProjectRef(project_id="alone"))
        standing = await standards.evaluate(ProjectRef(project_id="alone"))
        assert standing.error == "the forge repo was not found" and standing.report is None
    finally:
        await runtime.stop()


LIVE_AGENT = shutil.which("claude") is not None and os.environ.get("FORGE_DASHBOARD_LIVE_AGENT") == "1"


@pytest.mark.live
@pytest.mark.skipif(not LIVE_AGENT, reason="needs the claude CLI and FORGE_DASHBOARD_LIVE_AGENT=1 (spends tokens)")
async def test_live_claude_brings_a_messy_repo_up_to_standards(
    data_paths: DataPaths,
    tmp_path: Path,
    forge_repo: Path,
    make_project: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "pre-commit-home"))
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git_service = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git_service, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    standards = StandardsService(runtime, projects, jobs, checks, agents)
    runtime.register([jobs, scheduler, manifests, projects, git_service, checks, agents, standards, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")], "forge_repo": str(forge_repo)})
    config = AgentsConfig()
    config.kinds.standards.model = "haiku"
    config.kinds.standards.max_turns = 40
    await runtime.db.kv_put("config", "agents", config.model_dump(mode="json"))
    await runtime.start()
    try:
        messy = make_project("Messy", extra=NONCOMPLIANT)
        await discovery.add_project(PathInput(path=str(messy)))
        final = await jobs.wait((await standards.bring_up(ProjectRef(project_id="messy"))).job_id)
        [run] = (await agents.branch_runs(BranchRunsInput(project_id="messy", kind="standards"))).runs
        assert final.status is JobStatus.ATTENTION, f"{final.summary} {run.error}"
        assert run.compliance_before is not None and run.compliance_after is not None
        assert run.compliance_after.passing > run.compliance_before.passing
        worktree = Path(run.worktree)
        assert (worktree / "AGENTS.md").is_file()
        report = rules.evaluate(worktree, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head=None)
        assert _results(report)["agents-sections"] == "pass"
    finally:
        await runtime.stop()
