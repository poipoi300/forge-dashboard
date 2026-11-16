"""Worktrees, landing, and branch runs on real git: nothing is reset, lost, or left running with no way to stop it."""

import asyncio
import shutil
import stat
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import Git, write_files, write_hook

from forge_dashboard import gitops, landing, worktrees
from forge_dashboard.branchwork import INTERRUPTED, BranchRun, BranchRunState, BranchWork
from forge_dashboard.core.db import Database
from forge_dashboard.core.service import ActionError


def make_repo(tmp_path: Path, git: Git, name: str = "repo") -> Path:
    repo = git.init(tmp_path / name)
    git(repo, "config", "user.email", "me@example.com")
    git(repo, "config", "user.name", "Me")
    write_files(repo, {"README.md": "# repo\n", "shared.txt": "one\n"})
    git.commit_all(repo, "Initial")
    return Path(repo)


def commit_on_branch(git: Git, repo: Path, branch: str, files: dict[str, str]) -> str:
    """A commit on a new branch off HEAD; the checkout returns to where it was."""
    start = git(repo, "branch", "--show-current")
    git(repo, "switch", "-q", "-c", branch)
    write_files(repo, files)
    sha = git.commit_all(repo, f"Work on {branch}")
    git(repo, "switch", "-q", start)
    return str(sha)


# --- committing in a worktree ---


async def test_commit_all_concludes_a_merge_whose_tree_equals_head(tmp_path: Path, git: Git) -> None:
    repo = make_repo(tmp_path, git)
    commit_on_branch(git, repo, "a1", {"same.txt": "same\n"})
    other = commit_on_branch(git, repo, "a2", {"same.txt": "same\n"})
    git(repo, "switch", "-q", "a1")
    git(repo, "merge", "--no-ff", "--no-commit", "a2")
    tree_before = git(repo, "rev-parse", "HEAD^{tree}")

    merge_sha = await worktrees.commit_all(repo, "Sync")

    assert merge_sha is not None
    git(repo, "merge-base", "--is-ancestor", other, "HEAD")
    assert git(repo, "rev-parse", "HEAD^{tree}") == tree_before
    assert await worktrees.commit_all(repo, "Nothing left") is None


async def test_commit_all_raises_with_the_hooks_output_when_the_commit_fails_twice(tmp_path: Path, git: Git) -> None:
    repo = make_repo(tmp_path, git)
    hook = repo / ".git" / "hooks" / "pre-commit"
    write_hook(hook, "#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(gitops.GitError, match="hook says no"):
        await worktrees.commit_all(repo, "Blocked")


# --- removing and reconciling ---


async def test_remove_clears_read_only_files_and_does_it_off_the_event_loop(
    tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path, git)
    lost = tmp_path / "lost-worktree"
    (lost / "sub").mkdir(parents=True)
    locked = lost / "sub" / "locked.txt"
    locked.write_text("x", encoding="utf-8")
    locked.chmod(stat.S_IREAD)
    removing_threads: list[threading.Thread] = []
    real_rmtree = shutil.rmtree

    def spy(path: Path, onexc: Callable[..., Any]) -> None:
        removing_threads.append(threading.current_thread())
        real_rmtree(path, onexc=onexc)

    monkeypatch.setattr(shutil, "rmtree", spy)

    await worktrees.remove(worktrees.Worktree(repo=repo, path=lost, branch=None), delete_branch=False)

    assert not lost.exists()
    assert removing_threads and all(thread is not threading.main_thread() for thread in removing_threads)


async def test_reconcile_removes_unreferenced_worktrees_and_their_branches_and_keeps_referenced_ones(
    tmp_path: Path, git: Git
) -> None:
    repo = make_repo(tmp_path, git)
    root = tmp_path / "worktree-root"
    kept = await worktrees.create(
        repo, worktrees.worktree_path(root, "p", "forge-sync/kept"), base="main", branch="forge-sync/kept"
    )
    stray = await worktrees.create(
        repo, worktrees.worktree_path(root, "p", "forge-sync/stray"), base="main", branch="forge-sync/stray"
    )
    plain = root / "p" / "not-a-worktree"
    plain.mkdir(parents=True)
    (plain / "leftover.txt").write_text("x", encoding="utf-8")

    removed = await worktrees.reconcile(root, [kept.path], untouched_since=time.time() + 60)

    assert sorted(path.name for path in removed) == ["forge-sync-stray", "not-a-worktree"]
    assert kept.path.is_dir() and not stray.path.exists() and not plain.exists()
    assert git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/forge-sync") == "forge-sync/kept"
    assert "forge-sync-stray" not in git(repo, "worktree", "list")


async def test_reconcile_with_no_worktree_root_removes_nothing(tmp_path: Path) -> None:
    assert await worktrees.reconcile(tmp_path / "never-created", [], untouched_since=time.time()) == []


async def test_reconcile_leaves_directories_touched_since_this_process_began(tmp_path: Path, git: Git) -> None:
    repo = make_repo(tmp_path, git)
    root = tmp_path / "worktree-root"
    in_use = await worktrees.create(repo, worktrees.worktree_path(root, "p", "checks-now"), base="main", branch=None)

    removed = await worktrees.reconcile(root, [], untouched_since=time.time() - 3600)

    assert removed == [] and in_use.path.is_dir()


# --- landing ---


def landing_fixture(tmp_path: Path, git: Git) -> tuple[Path, str]:
    repo = make_repo(tmp_path, git)
    return repo, commit_on_branch(git, repo, "work", {"added.txt": "added\n"})


async def test_land_fast_forwards_a_branch_nobody_has_checked_out_by_compare_and_swap(tmp_path: Path, git: Git) -> None:
    repo, new_sha = landing_fixture(tmp_path, git)
    git(repo, "branch", "topic", "main")

    landed = await landing.land(repo, "topic", new_sha)

    assert (landed.outcome, landed.checkout) == ("updated", None)
    assert git(repo, "rev-parse", "topic") == new_sha


async def test_land_follows_a_clean_checkout_by_fast_forward(tmp_path: Path, git: Git) -> None:
    repo, new_sha = landing_fixture(tmp_path, git)

    landed = await landing.land(repo, "main", new_sha)

    assert landed.outcome == "updated" and landed.checkout == repo
    assert (repo / "added.txt").is_file()


async def test_land_parks_a_checkout_with_local_changes(tmp_path: Path, git: Git) -> None:
    repo, new_sha = landing_fixture(tmp_path, git)
    (repo / "shared.txt").write_text("my edit\n", encoding="utf-8")

    landed = await landing.land(repo, "main", new_sha)

    assert landed.outcome == "dirty"
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "my edit\n"


async def test_land_reports_gits_own_refusal_instead_of_calling_it_dirty(tmp_path: Path, git: Git) -> None:
    repo, new_sha = landing_fixture(tmp_path, git)
    (repo / "added.txt").write_text("mine, untracked\n", encoding="utf-8")

    landed = await landing.land(repo, "main", new_sha)

    assert landed.outcome == "refused"
    assert landed.detail is not None and "added.txt" in landed.detail and "overwritten" in landed.detail
    assert (repo / "added.txt").read_text(encoding="utf-8") == "mine, untracked\n"


async def test_a_rewrite_never_lands_into_a_checked_out_branch(tmp_path: Path, git: Git) -> None:
    repo, new_sha = landing_fixture(tmp_path, git)
    main_before = git(repo, "rev-parse", "main")

    landed = await landing.land(repo, "main", new_sha, rewrite=True)

    assert (landed.outcome, landed.checkout) == ("checked-out", repo)
    assert git(repo, "rev-parse", "main") == main_before
    assert not (repo / "added.txt").exists(), "the clean checkout is not reset"


async def test_a_rewrite_updates_a_branch_nobody_has_checked_out_even_when_history_is_replaced(
    tmp_path: Path, git: Git
) -> None:
    repo, work_tip = landing_fixture(tmp_path, git)
    git(repo, "branch", "topic", work_tip)
    replacement = commit_on_branch(git, repo, "elsewhere", {"other.txt": "o\n"})
    assert (await landing.land(repo, "topic", replacement)).outcome == "not-fast-forward"

    landed = await landing.land(repo, "topic", replacement, rewrite=True)

    assert landed.outcome == "updated" and git(repo, "rev-parse", "topic") == replacement


async def test_land_refuses_a_missing_branch_and_a_non_fast_forward(tmp_path: Path, git: Git) -> None:
    repo, new_sha = landing_fixture(tmp_path, git)
    assert (await landing.land(repo, "nope", new_sha)).outcome == "missing-branch"
    assert (await landing.land(repo, "work", git(repo, "rev-parse", "main"))).outcome == "not-fast-forward"


# --- branch runs ---


@pytest.fixture
async def branch_work(tmp_path: Path) -> AsyncIterator[BranchWork]:
    database = await Database.open(tmp_path / "branch.sqlite3")
    try:
        yield BranchWork(database, tmp_path / "worktrees")
    finally:
        await database.close()


async def start_run(branch_work: BranchWork, repo: Path) -> tuple[BranchRun, worktrees.Worktree]:
    return await branch_work.start(
        repo=repo,
        project_id="alpha",
        kind="delegate",
        title="Work",
        branch_prefix="forge-issue/1",
        base_branch="main",
        job_id="job",
    )


async def test_an_error_in_a_guarded_run_marks_it_failed_and_keeps_the_worktree(
    branch_work: BranchWork, tmp_path: Path, git: Git
) -> None:
    repo = make_repo(tmp_path, git)
    run, worktree = await start_run(branch_work, repo)
    changes: list[BranchRunState] = []

    async def on_change(changed: BranchRun) -> None:
        changes.append(changed.state)

    with pytest.raises(RuntimeError, match="boom"):
        async with branch_work.guarded(run, repo, on_change=on_change):
            raise RuntimeError("boom")

    failed = await branch_work.get(run.id)
    assert failed.state is BranchRunState.FAILED and failed.error == "RuntimeError: boom"
    assert worktree.path.is_dir() and changes == [BranchRunState.FAILED]
    discarded = await branch_work.discard(failed, repo)
    assert discarded.state is BranchRunState.DISCARDED and not worktree.path.exists()


async def test_cancelling_a_guarded_run_discards_it_with_its_worktree_and_branch(
    branch_work: BranchWork, tmp_path: Path, git: Git
) -> None:
    repo = make_repo(tmp_path, git)
    run, worktree = await start_run(branch_work, repo)

    with pytest.raises(asyncio.CancelledError):
        async with branch_work.guarded(run, repo):
            raise asyncio.CancelledError

    assert (await branch_work.get(run.id)).state is BranchRunState.DISCARDED
    assert not worktree.path.exists() and git(repo, "branch", "--list", "forge-issue/*") == ""


async def test_a_guarded_run_the_work_already_finished_keeps_its_recorded_state(
    branch_work: BranchWork, tmp_path: Path, git: Git
) -> None:
    repo = make_repo(tmp_path, git)
    run, worktree = await start_run(branch_work, repo)

    with pytest.raises(RuntimeError):
        async with branch_work.guarded(run, repo):
            await branch_work.save(run.model_copy(update={"state": BranchRunState.READY}))
            raise RuntimeError("after the work was done")

    assert (await branch_work.get(run.id)).state is BranchRunState.READY and worktree.path.is_dir()


async def test_discarding_a_running_run_is_refused(branch_work: BranchWork, tmp_path: Path, git: Git) -> None:
    repo = make_repo(tmp_path, git)
    run, worktree = await start_run(branch_work, repo)

    with pytest.raises(ActionError, match="still running"):
        await branch_work.discard(run, repo)

    assert worktree.path.is_dir() and (await branch_work.get(run.id)).state is BranchRunState.RUNNING


async def test_merge_reports_gits_own_refusal_not_a_request_to_stash(
    branch_work: BranchWork, tmp_path: Path, git: Git
) -> None:
    repo = make_repo(tmp_path, git)
    run, worktree = await start_run(branch_work, repo)
    (worktree.path / "added.txt").write_text("agent work\n", encoding="utf-8")
    git.commit_all(worktree.path, "Agent work")
    await branch_work.save(run.model_copy(update={"state": BranchRunState.READY}))
    (repo / "added.txt").write_text("mine, untracked\n", encoding="utf-8")

    with pytest.raises(ActionError) as refused:
        await branch_work.merge(run, repo)

    assert "git refused to move main" in refused.value.message and "added.txt" in refused.value.message
    assert "stash" not in refused.value.message
    assert (await branch_work.get(run.id)).state is BranchRunState.READY


async def test_runs_left_running_by_a_restart_fail_in_their_record_and_keep_their_worktrees(
    branch_work: BranchWork, tmp_path: Path, git: Git
) -> None:
    repo = make_repo(tmp_path, git)
    run, worktree = await start_run(branch_work, repo)

    await branch_work.fail_interrupted()

    interrupted = await branch_work.get(run.id)
    assert interrupted.state is BranchRunState.FAILED and interrupted.error == INTERRUPTED
    assert worktree.path.is_dir() and worktree.path in await branch_work.referenced_worktrees()
    await branch_work.discard(interrupted, repo)
    assert worktree.path not in await branch_work.referenced_worktrees()


async def test_a_start_that_cannot_record_the_run_removes_the_worktree_it_made(
    tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path, git)
    database = await Database.open(tmp_path / "failing.sqlite3")

    async def failing_insert(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(database, "execute", failing_insert)
    try:
        with pytest.raises(RuntimeError, match="disk full"):
            await start_run(BranchWork(database, tmp_path / "worktrees"), repo)
    finally:
        await database.close()

    assert not any((tmp_path / "worktrees").glob("*/*")) and git(repo, "branch", "--list", "forge-issue/*") == ""
