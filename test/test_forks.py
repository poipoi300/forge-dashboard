"""Fork sync end to end on fixture repos with a bare upstream, using the fake harness for conflicts."""

import os
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import MANIFEST, Git, write_files
from testing_support import fake_harness_config

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, ProjectRef
from forge_dashboard.domain import forks as rules
from forge_dashboard.domain.forks import ForkPolicy, ForkRunState
from forge_dashboard.domain.scheduling import IntervalSchedule
from forge_dashboard.services.agents import AgentsConfig, AgentsService
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.forks import (
    DiffInput,
    ForksService,
    HistoryInput,
    PolicyInput,
    PreviewInput,
    RetryInput,
    RunInput,
)
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobsService, JobStatus
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


def build_fork(tmp_path: Path, git: Git, *, conflict: bool, manifest_extra: str = "", flag: bool = True) -> ForkRepos:
    """An upstream repo, a fork of it on a bare origin, and the user's checkout with `upstream` configured."""
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

    (checkout / "shared.txt").write_text(
        "line one\nfork's line two\nline three\n" if conflict else "line one\nline two\nline three\nfork tail\n",
        encoding="utf-8",
        newline="\n",
    )
    git.commit_all(checkout, "Fork change")
    git(upstream_work, "remote", "add", "origin", str(upstream_bare))
    (upstream_work / "shared.txt").write_text(
        "line one\nupstream's line two\nline three\n"
        if conflict
        else "upstream head\nline one\nline two\nline three\n",
        encoding="utf-8",
        newline="\n",
    )
    (upstream_work / "upstream-only.txt").write_text("new upstream file\n", encoding="utf-8")
    git.commit_all(upstream_work, "Upstream change")
    git(upstream_work, "push", "-q", "origin", "main")
    return ForkRepos(
        checkout=checkout, upstream_work=upstream_work, upstream_bare=upstream_bare, origin_bare=origin_bare
    )


@pytest.fixture
async def forks_env(
    data_paths: DataPaths, tmp_path: Path
) -> AsyncIterator[tuple[ForksService, ProjectsService, DiscoveryService, JobsService, SchedulerService]]:
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
    await runtime.db.kv_put("config", "agents", AgentsConfig(harness=fake_harness_config()).model_dump(mode="json"))
    await runtime.start()
    try:
        yield forks, projects, discovery, jobs, scheduler
    finally:
        await runtime.stop()


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
    status, _summary = await _sync(forks, jobs)
    assert status is JobStatus.SUCCEEDED
    upstream_sha = git(repos.upstream_bare, "rev-parse", "main")
    assert git(repos.checkout, "rev-parse", "HEAD~1") == upstream_sha, "fork commits are replayed on upstream"
    note = git(repos.checkout, "notes", "--ref=forge-sync", "show", "HEAD")
    assert f"Forge-Sync-Upstream: {upstream_sha}" in note


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
