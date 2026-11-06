"""Fork sync end to end on fixture repos with a bare upstream, using the fake harness for conflicts."""

import asyncio
import os
import shutil
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import MANIFEST, Git, write_files
from pydantic import ValidationError
from testing_support import fake_harness_config

from forge_dashboard import worktrees
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, ProjectRef
from forge_dashboard.domain import forks as rules
from forge_dashboard.domain.forks import ForkPolicy, ForkRun, ForkRunState
from forge_dashboard.domain.harness import HarnessConfig
from forge_dashboard.domain.scheduling import IntervalSchedule
from forge_dashboard.services.agents import AgentsConfig, AgentsService
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.forks import (
    DiffInput,
    ForksConfig,
    ForksService,
    HistoryInput,
    PolicyInput,
    PreviewInput,
    RetryInput,
    RunInput,
    _Round,
    _Sync,
)
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobContext, JobIdInput, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService

PYTHON = sys.executable.replace(chr(92), "/")
FAILING_CHECK = f"""
[[checks]]
id = "gate"
label = "Gate"
argv = ["{PYTHON}", "-c", "import pathlib, sys; sys.exit(0 if pathlib.Path('pass.flag').exists() else 1)"]
stage = "commit"
"""


@dataclass
class ForkRepos:
    checkout: Path
    upstream_work: Path
    upstream_bare: Path
    origin_bare: Path


def build_fork(
    tmp_path: Path, git: Git, *, conflict: bool, manifest_extra: str = "", flag: bool = True, identical: bool = False
) -> ForkRepos:
    """An upstream repo, a fork of it on a bare origin, and the user's checkout with `upstream` configured.

    With `identical`, the fork and upstream make the very same change in different commits, so merging
    upstream into the fork produces a tree equal to the fork's own.
    """
    upstream_work = tmp_path / "upstream-work"
    files = {
        "forge.project.toml": MANIFEST.replace("{name}", "Forked").replace("{project_id}", "forked") + manifest_extra,
        "shared.txt": "line one\nline two\nline three\n",
        "settings.toml": 'name = "base"\n',
    }
    if flag:
        files["pass.flag"] = ""
    write_files(upstream_work, files)
    git.init(upstream_work)
    git.commit_all(upstream_work, "Initial upstream")
    upstream_bare = tmp_path / "upstream.git"
    git(tmp_path, "clone", "-q", "--bare", str(upstream_work), str(upstream_bare))
    origin_bare = tmp_path / "fork.git"
    git(tmp_path, "clone", "-q", "--bare", str(upstream_bare), str(origin_bare))
    checkout = tmp_path / "projects" / "Forked"
    git(tmp_path, "clone", "-q", str(origin_bare), str(checkout))
    git(checkout, "remote", "add", "upstream", str(upstream_bare))
    git(checkout, "config", "user.email", "me@example.com")
    git(checkout, "config", "user.name", "Me")

    same_change = "line one\nline two\nline three\nsame tail\n"
    fork_text = same_change if identical else "line one\nfork's line two\nline three\n"
    (checkout / "shared.txt").write_text(
        fork_text if identical or conflict else "line one\nline two\nline three\nfork tail\n",
        encoding="utf-8",
        newline="\n",
    )
    git.commit_all(checkout, "Fork change")
    git(upstream_work, "remote", "add", "origin", str(upstream_bare))
    upstream_text = (
        "line one\nupstream's line two\nline three\n" if conflict else "upstream head\nline one\nline two\nline three\n"
    )
    (upstream_work / "shared.txt").write_text(
        same_change if identical else upstream_text, encoding="utf-8", newline="\n"
    )
    if not identical:
        (upstream_work / "upstream-only.txt").write_text("new upstream file\n", encoding="utf-8")
    git.commit_all(upstream_work, "Upstream change")
    git(upstream_work, "push", "-q", "origin", "main")
    return ForkRepos(
        checkout=checkout, upstream_work=upstream_work, upstream_bare=upstream_bare, origin_bare=origin_bare
    )


ForksEnv = tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService]


@asynccontextmanager
async def _forks_runtime(data_paths: DataPaths, tmp_path: Path, harness: HarnessConfig) -> AsyncIterator[ForksEnv]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git_service = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git_service, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    forks = ForksService(runtime, projects, git_service, jobs, scheduler, checks, agents)
    runtime.register([jobs, scheduler, manifests, projects, git_service, checks, agents, forks, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")]})
    await runtime.db.kv_put("config", "agents", AgentsConfig(harness=harness).model_dump(mode="json"))
    await runtime.start()
    try:
        yield forks, projects, discovery, jobs, scheduler
    finally:
        await runtime.stop()


@pytest.fixture
async def forks_env(data_paths: DataPaths, tmp_path: Path) -> AsyncIterator[ForksEnv]:
    async with _forks_runtime(data_paths, tmp_path, fake_harness_config()) as env:
        yield env


@pytest.fixture
async def hanging_forks_env(data_paths: DataPaths, tmp_path: Path) -> AsyncIterator[ForksEnv]:
    """Forks wired to an agent that never finishes, for tests that cancel a run mid-flight."""
    hang = HarnessConfig(command=[sys.executable, "-c", "import time; time.sleep(120)"], stdin=None)
    async with _forks_runtime(data_paths, tmp_path, hang) as env:
        yield env


async def _sync(forks: ForksService, jobs: JobsService) -> tuple[JobStatus, str | None]:
    final = await jobs.wait((await forks.sync(ProjectRef(project_id="forked"))).job_id)
    return final.status, final.summary


async def test_clean_upstream_changes_merge_without_an_agent(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    git(repos.checkout, "fetch", "-q", "upstream")
    preview = await forks.preview(PreviewInput(project_id="forked"))
    assert (preview.status.behind, preview.status.ahead) == (1, 1)
    assert preview.status.prediction is not None and preview.status.prediction.clean
    assert [c.subject for c in preview.commits] == ["Upstream change"]
    assert {f.path for f in preview.files} == {"shared.txt", "upstream-only.txt"}

    status, summary = await _sync(forks, jobs)
    assert status is JobStatus.SUCCEEDED and summary == "main updated; no conflicts"
    assert (repos.checkout / "upstream-only.txt").exists(), "the clean checkout fast-forwards"
    message = git(repos.checkout, "log", "-1", "--format=%B")
    assert "Forge-Sync-Upstream: " in message and "Forge-Sync-Agent: no" in message
    assert git(repos.checkout, "branch", "--list", "forge-sync/*") == ""
    fork = projects.summary("forked").fork
    assert fork is not None and fork.last_sync == "synced" and fork.behind_upstream == 0

    assert (await _sync(forks, jobs))[1] == "main already contains upstream/main"


async def test_conflicts_are_resolved_by_the_harness_and_recorded_in_the_commit(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    git(repos.checkout, "fetch", "-q", "upstream")
    preview = await forks.preview(PreviewInput(project_id="forked"))
    assert preview.status.prediction is not None and preview.status.prediction.conflicted_files == ["shared.txt"]
    assert (repos.checkout / "shared.txt").read_text(encoding="utf-8").count("fork's") == 1

    status, summary = await _sync(forks, jobs)
    assert status is JobStatus.SUCCEEDED and summary == "main updated; agent resolved conflicts in 1 attempt(s)"
    merged = (repos.checkout / "shared.txt").read_text(encoding="utf-8")
    assert "fork's line two" in merged and "upstream's line two" in merged
    assert rules.is_conflict_marker_free(merged)
    assert "Forge-Sync-Agent: yes" in git(repos.checkout, "log", "-1", "--format=%B")
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert (run.state, run.agent_attempts, run.conflicted_files) == (ForkRunState.SYNCED, 1, ["shared.txt"])
    assert run.agent_cost_usd == pytest.approx(0.0123) and run.agent_turns == 1


async def test_failed_verification_retries_with_the_failure_appended(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    monkeypatch.setenv("FAKE_HARNESS_SABOTAGE_FIRST", "1")
    status, _summary = await _sync(forks, jobs)
    assert status is JobStatus.SUCCEEDED
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.agent_attempts == 2


async def test_stop_policy_parks_the_conflict_and_abort_cleans_up(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks.set_policy(PolicyInput(project_id="forked", policy=ForkPolicy(conflicts="stop")))
    status, summary = await _sync(forks, jobs)
    assert status is JobStatus.ATTENTION and summary == "conflict: 1 conflicted file"
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.state is ForkRunState.CONFLICT and run.worktree is not None and Path(run.worktree).is_dir()
    assert projects.summary("forked").attention[-1].message == "fork sync conflict"
    diff = await forks.run_diff(DiffInput(run_id=run.id, against="target"))
    assert "upstream-only.txt" in diff.diff
    await forks.abort(RunInput(run_id=run.id))
    assert not Path(run.worktree).exists()
    assert git(repos.checkout, "branch", "--list", "forge-sync/*") == ""
    with pytest.raises(ActionError, match="not parked"):
        await forks.abort(RunInput(run_id=run.id))


async def test_a_parked_conflict_can_be_retried_with_instructions(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks.set_policy(PolicyInput(project_id="forked", policy=ForkPolicy(conflicts="stop")))
    await _sync(forks, jobs)
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    retried = await jobs.wait((await forks.retry_agent(RetryInput(run_id=run.id, instructions="Keep both"))).job_id)
    assert retried.status is JobStatus.SUCCEEDED
    assert "upstream's line two" in (repos.checkout / "shared.txt").read_text(encoding="utf-8")


async def test_dirty_checkouts_park_as_ready_to_apply_until_clean(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    (repos.checkout / "settings.toml").write_text('name = "my local edit"\n', encoding="utf-8")
    status, summary = await _sync(forks, jobs)
    assert status is JobStatus.ATTENTION and summary is not None and summary.startswith("ready-to-apply")
    assert (repos.checkout / "settings.toml").read_text(encoding="utf-8") == 'name = "my local edit"\n'
    assert not (repos.checkout / "upstream-only.txt").exists()
    git(repos.checkout, "checkout", "--", "settings.toml")
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    applied = await jobs.wait((await forks.apply(RunInput(run_id=run.id))).job_id)
    assert applied.status is JobStatus.SUCCEEDED
    assert (repos.checkout / "upstream-only.txt").exists()


async def test_failing_checks_park_the_run_and_keep_the_branch(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False, manifest_extra=FAILING_CHECK, flag=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    status, summary = await _sync(forks, jobs)
    assert status is JobStatus.ATTENTION and summary == "needs-attention: checks failing: Gate"
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.checks == [("gate", "failed")] and run.branch is not None
    assert git(repos.checkout, "rev-parse", "--verify", f"refs/heads/{run.branch}")
    assert not (repos.checkout / "upstream-only.txt").exists()


async def test_rebase_strategy_replays_the_fork_and_leaves_a_note(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks.set_policy(PolicyInput(project_id="forked", policy=ForkPolicy(strategy="rebase")))
    git(repos.checkout, "switch", "-q", "-c", "elsewhere")  # a rewrite only lands on a branch nobody has checked out
    status, _summary = await _sync(forks, jobs)
    assert status is JobStatus.SUCCEEDED
    upstream_sha = git(repos.upstream_bare, "rev-parse", "main")
    assert git(repos.checkout, "rev-parse", "main~1") == upstream_sha, "fork commits are replayed on upstream"
    note = git(repos.checkout, "notes", "--ref=forge-sync", "show", "main")
    assert f"Forge-Sync-Upstream: {upstream_sha}" in note


async def test_a_rebase_never_resets_a_checked_out_branch_and_applies_once_it_is_free(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks.set_policy(PolicyInput(project_id="forked", policy=ForkPolicy(strategy="rebase")))
    main_before = git(repos.checkout, "rev-parse", "main")

    status, summary = await _sync(forks, jobs)

    assert status is JobStatus.ATTENTION and summary is not None and summary.startswith("ready-to-apply")
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.state is ForkRunState.READY_TO_APPLY and "check out another branch" in (run.detail or "")
    assert git(repos.checkout, "rev-parse", "main") == main_before, "the checked-out branch is not rewritten"
    assert not (repos.checkout / "upstream-only.txt").exists(), "the user's working tree is not reset"

    git(repos.checkout, "switch", "-q", "-c", "elsewhere")
    applied = await jobs.wait((await forks.apply(RunInput(run_id=run.id))).job_id)
    assert applied.status is JobStatus.SUCCEEDED
    assert git(repos.checkout, "rev-parse", "main~1") == git(repos.upstream_bare, "rev-parse", "main")


async def test_a_ref_update_refused_by_git_parks_with_gits_own_message(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    (repos.checkout / "upstream-only.txt").write_text("mine, untracked\n", encoding="utf-8")  # blocks the fast-forward

    status, summary = await _sync(forks, jobs)

    assert status is JobStatus.ATTENTION and summary is not None and summary.startswith("ready-to-apply")
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.state is ForkRunState.READY_TO_APPLY
    assert "git refused to update main" in (run.detail or "") and "upstream-only.txt" in (run.detail or "")
    assert (repos.checkout / "upstream-only.txt").read_text(encoding="utf-8") == "mine, untracked\n"


async def test_a_merge_git_rejects_fails_the_run_instead_of_landing_nothing(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    unrelated = tmp_path / "unrelated-work"
    write_files(unrelated, {"unrelated.txt": "nothing in common\n"})
    git.init(unrelated)
    git.commit_all(unrelated, "Unrelated root")
    unrelated_bare = tmp_path / "unrelated.git"
    git(tmp_path, "clone", "-q", "--bare", str(unrelated), str(unrelated_bare))
    git(repos.checkout, "remote", "set-url", "upstream", str(unrelated_bare))
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    main_before = git(repos.checkout, "rev-parse", "main")

    status, summary = await _sync(forks, jobs)

    assert status is JobStatus.FAILED and "unrelated histories" in (summary or "")
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.state is ForkRunState.FAILED and run.worktree is None and "unrelated histories" in (run.detail or "")
    assert git(repos.checkout, "rev-parse", "main") == main_before
    assert git(repos.checkout, "branch", "--list", "forge-sync/*") == ""
    assert not any(forks.runtime.paths.worktrees.glob("*/*")), "the failed run's worktree is removed"


async def test_a_rebase_git_refuses_to_start_fails_the_run_instead_of_landing_an_unrebased_tree(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks.set_policy(PolicyInput(project_id="forked", policy=ForkPolicy(strategy="rebase")))
    hook = repos.checkout / ".git" / "hooks" / "pre-rebase"
    hook.write_text("#!/bin/sh\necho 'rebase refused' >&2\nexit 1\n", encoding="utf-8", newline="\n")
    main_before = git(repos.checkout, "rev-parse", "main")

    status, summary = await _sync(forks, jobs)

    assert status is JobStatus.FAILED and "git rebase failed" in (summary or "") and "refused" in (summary or "")
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.state is ForkRunState.FAILED and run.worktree is None and run.result_sha is None
    assert git(repos.checkout, "branch", "--list", "forge-sync/*") == ""
    assert git(repos.checkout, "rev-parse", "main") == main_before


async def test_a_rebase_continue_that_fails_reports_gits_output_as_a_failure_not_a_clean_round(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    worktree = await worktrees.create(repos.checkout, tmp_path / "not-mid-rebase", base="main", branch=None)
    sync = _Sync(
        record=projects.record("forked"), policy=ForkPolicy(strategy="rebase"), run=_stored_run(), worktree=worktree
    )
    rounds: list[_Round] = []

    async def work(context: JobContext) -> JobOutcome:
        rounds.append(await forks._continue(context, sync))
        return JobOutcome()

    await jobs.wait((await jobs.submit(kind="t", title="t", work=work)).job_id)

    [step] = rounds
    assert step.conflicted == [] and step.failure is not None
    assert step.failure.startswith("git rebase failed") and "no rebase in progress" in step.failure


async def test_a_merge_whose_tree_equals_the_target_still_records_the_merge(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False, identical=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    tree_before = git(repos.checkout, "rev-parse", "main^{tree}")

    status, summary = await _sync(forks, jobs)

    assert status is JobStatus.SUCCEEDED and summary == "main updated; no conflicts"
    upstream_sha = git(repos.upstream_bare, "rev-parse", "main")
    git(repos.checkout, "merge-base", "--is-ancestor", upstream_sha, "main")
    assert git(repos.checkout, "rev-parse", "main^{tree}") == tree_before, "the merge adds no content"
    assert "Forge-Sync-Upstream: " in git(repos.checkout, "log", "-1", "--format=%B", "main")
    assert (await _sync(forks, jobs))[1] == "main already contains upstream/main", "it does not repeat forever"


async def test_a_result_that_lacks_upstream_is_never_landed(forks_env: ForksEnv, tmp_path: Path, git: Git) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks.set_policy(PolicyInput(project_id="forked", policy=ForkPolicy(conflicts="stop")))
    main_before = git(repos.checkout, "rev-parse", "main")
    await _sync(forks, jobs)
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.worktree is not None
    git(Path(run.worktree), "merge", "--abort")  # the maintainer backs the merge out by hand, then clicks Apply

    applied = await jobs.wait((await forks.apply(RunInput(run_id=run.id))).job_id)

    assert applied.status is JobStatus.ATTENTION and "does not contain upstream" in (applied.summary or "")
    [after] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert after.state is ForkRunState.NEEDS_ATTENTION
    assert git(repos.checkout, "rev-parse", "main") == main_before


def _block_commits(checkout: Path) -> Path:
    """A pre-commit hook in the user's repo (shared by its worktrees) that rejects every commit."""
    hook = checkout / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n", encoding="utf-8", newline="\n")
    return hook


async def test_a_failing_commit_hook_parks_the_run_and_applying_it_later_lands(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    hook = _block_commits(repos.checkout)

    final = await jobs.wait((await forks.sync(ProjectRef(project_id="forked"))).job_id)

    assert final.status is JobStatus.FAILED and "hook says no" in (final.error or "")
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.state is ForkRunState.NEEDS_ATTENTION and "hook says no" in (run.detail or "")
    assert run.worktree is not None and Path(run.worktree).is_dir(), "the work is kept for inspection"
    assert not (repos.checkout / "upstream-only.txt").exists()

    hook.unlink()
    applied = await jobs.wait((await forks.apply(RunInput(run_id=run.id))).job_id)
    assert applied.status is JobStatus.SUCCEEDED
    assert (repos.checkout / "upstream-only.txt").exists()


async def test_a_run_stopped_by_an_error_can_be_aborted(forks_env: ForksEnv, tmp_path: Path, git: Git) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    _block_commits(repos.checkout)
    await jobs.wait((await forks.sync(ProjectRef(project_id="forked"))).job_id)
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.worktree is not None

    await forks.abort(RunInput(run_id=run.id))

    assert not Path(run.worktree).exists() and git(repos.checkout, "branch", "--list", "forge-sync/*") == ""
    [after] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert after.state is ForkRunState.ABORTED


async def _until_state_is_quiet(forks: ForksService) -> None:
    previous = None
    while (current := forks.state.model_dump_json()) != previous:
        previous = current
        await asyncio.sleep(1.5)


async def _eventually(check: Callable[[], Awaitable[bool]], timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout_s}s")
        await asyncio.sleep(0.05)


async def test_cancelling_a_sync_aborts_the_run_and_removes_its_worktree_and_branch(
    hanging_forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = hanging_forks_env
    repos = build_fork(tmp_path, git, conflict=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    ref = await forks.sync(ProjectRef(project_id="forked"))

    async def agent_is_running() -> bool:
        runs = (await forks.history(HistoryInput(project_id="forked"))).runs
        return bool(runs) and runs[0].conflicted_files == ["shared.txt"]

    await _eventually(agent_is_running)
    await jobs.cancel(JobIdInput(job_id=ref.job_id))

    assert (await jobs.wait(ref.job_id)).status is JobStatus.CANCELLED
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert run.state is ForkRunState.ABORTED and run.worktree is None
    assert git(repos.checkout, "branch", "--list", "forge-sync/*") == ""
    assert not any(forks.runtime.paths.worktrees.glob("*/*"))


def _stored_run(**changes: object) -> ForkRun:
    run = ForkRun(
        id="run",
        project_id="forked",
        job_id="job",
        state=ForkRunState.RUNNING,
        strategy="merge",
        upstream_ref="upstream/main",
        upstream_sha="a" * 40,
        target_branch="main",
        started_at=time.time(),
    )
    return run.model_copy(update=changes)


async def test_runs_interrupted_by_a_restart_are_parked_or_failed_in_their_stored_record(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, _jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    path = worktrees.worktree_path(forks.runtime.paths.worktrees, "forked", "forge-sync/main-crashed")
    await worktrees.create(repos.checkout, path, base="main", branch="forge-sync/main-crashed")
    await forks._save(_stored_run(id="with-worktree", branch="forge-sync/main-crashed", worktree=str(path)))
    await forks._save(_stored_run(id="without-worktree", started_at=time.time() + 1))

    await forks.start()

    parked = await forks._run("with-worktree")
    assert parked.state is ForkRunState.NEEDS_ATTENTION and parked.detail == "interrupted by a dashboard restart"
    assert parked.ended_at is not None and parked.worktree == str(path)
    failed = await forks._run("without-worktree")
    assert failed.state is ForkRunState.FAILED and failed.worktree is None
    columns = await forks.runtime.db.fetch_all("SELECT id, state FROM fork_runs ORDER BY id")
    assert [(row["id"], row["state"]) for row in columns] == [
        ("with-worktree", "needs-attention"),
        ("without-worktree", "failed"),
    ]
    await forks.abort(RunInput(run_id="with-worktree"))
    assert not path.exists() and git(repos.checkout, "branch", "--list", "forge-sync/*") == ""


async def test_worktree_directories_no_run_references_are_removed_at_startup(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=True)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks.set_policy(PolicyInput(project_id="forked", policy=ForkPolicy(conflicts="stop")))
    await _sync(forks, jobs)
    [parked] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert parked.worktree is not None
    root = forks.runtime.paths.worktrees
    stray = worktrees.worktree_path(root, "forked", "forge-sync/main-stray")
    await worktrees.create(repos.checkout, stray, base="main", branch="forge-sync/main-stray")
    plain = root / "forked" / "not-a-worktree"
    plain.mkdir(parents=True)
    (plain / "leftover.txt").write_text("x", encoding="utf-8")
    forks._constructed_at = (
        time.time() + 60
    )  # as if the process that made these directories ended before this one began

    await forks.start()

    assert not stray.exists() and not plain.exists()
    assert Path(parked.worktree).is_dir(), "a parked run's worktree is referenced and stays"
    assert git(repos.checkout, "branch", "--list", "forge-sync/main-stray") == ""
    assert "main-stray" not in git(repos.checkout, "worktree", "list")


async def test_a_second_apply_queued_behind_a_busy_repo_finds_the_run_already_settled(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    (repos.checkout / "settings.toml").write_text('name = "my local edit"\n', encoding="utf-8")
    await _sync(forks, jobs)
    git(repos.checkout, "checkout", "--", "settings.toml")
    [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
    release = asyncio.Event()

    async def hold_the_repo(_context: JobContext) -> JobOutcome:
        await release.wait()
        return JobOutcome()

    busy = await jobs.submit(kind="t", title="hold", work=hold_the_repo, resources=["repo:forked"])
    first = await forks.apply(RunInput(run_id=run.id))
    second = await forks.apply(RunInput(run_id=run.id))
    release.set()

    await jobs.wait(busy.job_id)
    assert (await jobs.wait(first.job_id)).status is JobStatus.SUCCEEDED
    late = await jobs.wait(second.job_id)
    assert late.status is JobStatus.FAILED and "not parked" in (late.error or "")
    [after] = (await forks.history(HistoryInput(project_id="forked"))).runs
    assert after.state is ForkRunState.SYNCED, "the late apply did not overwrite the settled run"


async def test_finished_runs_beyond_keep_runs_are_pruned_but_runs_owning_a_worktree_stay(
    forks_env: ForksEnv,
) -> None:
    forks, _projects, _discovery, _jobs, _scheduler = forks_env
    await forks.update_config(ForksConfig(keep_runs=10))
    now = time.time()
    await forks._save(_stored_run(id="old-parked", state=ForkRunState.CONFLICT, started_at=now - 1000))
    for index in range(12):
        await forks._save(_stored_run(id=f"finished-{index:02d}", state=ForkRunState.SYNCED, started_at=now + index))

    kept = [run.id for run in (await forks.history(HistoryInput(project_id="forked", limit=500))).runs]

    assert "old-parked" in kept
    assert sorted(run_id for run_id in kept if run_id.startswith("finished")) == [
        f"finished-{index:02d}" for index in range(2, 12)
    ]


async def test_auto_push_and_schedules(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, projects, discovery, jobs, scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    policy = ForkPolicy(auto_push=True, schedule=IntervalSchedule(every_s=3600))
    await forks.set_policy(PolicyInput(project_id="forked", policy=policy))
    entry = scheduler.entry("forks:forked")
    assert entry is not None and entry.target_action == "sync" and entry.next_run_at is not None
    status, _summary = await _sync(forks, jobs)
    assert status is JobStatus.SUCCEEDED
    assert git(repos.origin_bare, "rev-parse", "main") == git(repos.checkout, "rev-parse", "HEAD")
    fork = projects.summary("forked").fork
    assert fork is not None and fork.next_sync_at == entry.next_run_at
    await forks.set_policy(PolicyInput(project_id="forked", policy=ForkPolicy()))
    assert scheduler.entry("forks:forked") is None


async def test_targets_not_checked_out_update_by_ref_and_upstream_urls_add_the_remote(
    forks_env: tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService],
    tmp_path: Path,
    git: Git,
) -> None:
    forks, _projects, discovery, jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    git(repos.checkout, "remote", "remove", "upstream")
    git(repos.checkout, "switch", "-q", "-c", "elsewhere")
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    refused = await jobs.wait((await forks.sync(ProjectRef(project_id="forked"))).job_id)
    assert refused.status is JobStatus.FAILED and "no upstream" in (refused.error or "")
    policy = ForkPolicy(upstream_url=str(repos.upstream_bare), target_branch="main")
    status = await forks.set_policy(PolicyInput(project_id="forked", policy=policy))
    assert status.is_fork and status.source == "upstream-remote"
    sync_status, _summary = await _sync(forks, jobs)
    assert sync_status is JobStatus.SUCCEEDED
    assert git(repos.checkout, "rev-parse", "main~0") != git(repos.checkout, "rev-parse", "elsewhere")
    assert "upstream-only.txt" in git(repos.checkout, "ls-tree", "--name-only", "main")
    assert not (repos.checkout / "upstream-only.txt").exists(), "the other branch's checkout is untouched"


def test_conflict_brief_lists_files_sides_commits_and_failures() -> None:
    brief = rules.conflict_brief(
        strategy="merge",
        upstream_ref="upstream/main",
        upstream_sha="a" * 40,
        target_branch="main",
        files=[
            rules.ConflictedFile(
                path="x.txt", merged="<<<<<<< ours\na\n=======\nb\n>>>>>>> theirs\n", ours="a", theirs="b"
            )
        ],
        upstream_commits=[],
        fork_commits=[],
        agents_md="Rules here",
        previous_failure="x.txt: conflict markers remain",
        extra_instructions="Prefer upstream",
    )
    assert brief.startswith("# Conflict brief")
    for fragment in (
        "### File: x.txt",
        "Upstream's side (theirs)",
        "Rules here",
        "Prefer upstream",
        "Previous attempt",
    ):
        assert fragment in brief
    assert rules.parse_problems(Path("a.toml"), "x = = 1") is not None
    assert rules.parse_problems(Path("a.json"), "{") is not None
    assert rules.parse_problems(Path("a.yaml"), "a: [") is not None
    assert rules.parse_problems(Path("a.py"), "def (") is None


LIVE_AGENT = shutil.which("claude") is not None and os.environ.get("FORGE_DASHBOARD_LIVE_AGENT") == "1"


@pytest.mark.live
@pytest.mark.skipif(not LIVE_AGENT, reason="needs the claude CLI and FORGE_DASHBOARD_LIVE_AGENT=1 (spends tokens)")
async def test_live_claude_resolves_a_real_conflict(data_paths: DataPaths, tmp_path: Path, git: Git) -> None:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git_service = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git_service, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    forks = ForksService(runtime, projects, git_service, jobs, scheduler, checks, agents)
    runtime.register([jobs, scheduler, manifests, projects, git_service, checks, agents, forks, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")]})
    config = AgentsConfig()
    config.kinds.fork_sync.model = "haiku"
    config.kinds.fork_sync.max_turns = 20
    await runtime.db.kv_put("config", "agents", config.model_dump(mode="json"))
    await runtime.start()
    try:
        repos = build_fork(tmp_path, git, conflict=True)
        await discovery.add_project(PathInput(path=str(repos.checkout)))
        status, summary = await _sync(forks, jobs)
        [run] = (await forks.history(HistoryInput(project_id="forked"))).runs
        assert status is JobStatus.SUCCEEDED, f"{summary}: {run.detail}"
        merged = (repos.checkout / "shared.txt").read_text(encoding="utf-8")
        assert rules.is_conflict_marker_free(merged)
        assert "Forge-Sync-Agent: yes" in git(repos.checkout, "log", "-1", "--format=%B")
        assert run.agent_attempts >= 1 and run.agent_cost_usd is not None
    finally:
        await runtime.stop()


async def test_a_parked_run_without_a_worktree_cannot_be_acted_on(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, _projects, discovery, _jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks._save(_stored_run(id="lost", state=ForkRunState.CONFLICT))

    for act in (forks.apply, forks.abort):
        with pytest.raises(ActionError, match="no worktree"):
            await act(RunInput(run_id="lost"))


async def test_preview_fetches_nothing_and_changes_no_service_state(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, projects, discovery, _jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    await forks.refresh(projects.record("forked"))
    before = forks.state.model_dump_json()

    unfetched = await forks.preview(PreviewInput(project_id="forked"))

    assert "fetch" not in PreviewInput.model_fields
    assert unfetched.status.error == "upstream/main has not been fetched yet" and unfetched.commits == []
    assert git(repos.checkout, "rev-parse", "--verify", "-q", "refs/remotes/upstream/main", check=False) == ""
    assert forks.state.model_dump_json() == before

    refreshed = await forks.refresh_status(ProjectRef(project_id="forked"))
    assert refreshed.fetched_at is not None and (refreshed.behind, refreshed.ahead) == (1, 1)
    await _until_state_is_quiet(forks)  # the git watcher refreshes after a fetch, on its own debounce
    settled = forks.state.model_dump_json()
    preview = await forks.preview(PreviewInput(project_id="forked"))
    assert preview.status.prediction is not None and preview.status.prediction.clean
    assert [commit.subject for commit in preview.commits] == ["Upstream change"]
    assert forks.state.model_dump_json() == settled, "reading the preview must not move the state it follows"


def test_fork_policies_refuse_upstream_urls_that_embed_credentials_or_look_like_options() -> None:
    for url in (
        "https://me:s3cret@github.com/o/r.git",
        "https://token@github.com/o/r.git",
        "ssh://git:pw@host/o/r.git",
        "--upload-pack=touch pwned",
    ):
        with pytest.raises(ValidationError):
            ForkPolicy(upstream_url=url)
    login_only = "ssh://git@github.com/o/r.git"
    assert ForkPolicy(upstream_url=login_only).upstream_url == login_only


async def test_an_upstream_remote_with_credentials_is_published_without_them(
    forks_env: ForksEnv, tmp_path: Path, git: Git
) -> None:
    forks, projects, discovery, _jobs, _scheduler = forks_env
    repos = build_fork(tmp_path, git, conflict=False)
    await discovery.add_project(PathInput(path=str(repos.checkout)))
    git(repos.checkout, "fetch", "-q", "upstream")
    git(repos.checkout, "remote", "set-url", "upstream", "https://me:s3cret@127.0.0.1:9/up.git")

    status = await forks.refresh(projects.record("forked"))

    assert status.upstream_url == "https://127.0.0.1:9/up.git"
    fork_facts = projects.summary("forked").fork
    assert fork_facts is not None and fork_facts.upstream == "https://127.0.0.1:9/up.git"
    assert "s3cret" not in status.model_dump_json() + forks.state.model_dump_json()
