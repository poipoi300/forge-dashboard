"""Isolated git worktrees under the dashboard's data dir, where every automated write happens.

A worktree shares the repository's object store and refs but has its own
checkout, so syncs, bring-ups, and HEAD-only check runs never touch the
user's working tree and never need to stash anything.
"""

from __future__ import annotations

import asyncio
import shutil
import stat
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_dashboard import gitops
from forge_dashboard.core.service import ActionError
from forge_dashboard.process import tree


@dataclass(frozen=True)
class Worktree:
    repo: Path
    path: Path
    branch: str | None


def timestamp() -> str:
    """A sortable, filesystem-safe stamp for worktree and branch names."""
    return time.strftime("%Y%m%d-%H%M%S")


def worktree_path(root: Path, project_id: str, name: str) -> Path:
    return root / project_id / name.replace("/", "-")


def describe_failure(error: Exception) -> str:
    """How a run records the exception that ended it: an action's own message, else the type and message."""
    return error.message if isinstance(error, ActionError) else f"{type(error).__name__}: {error}"


async def create(repo: Path, path: Path, *, base: str, branch: str | None) -> Worktree:
    """Add a worktree at `path` checked out at `base`, on a new `branch` or detached when None.

    Raises:
        gitops.GitError: when git refuses (branch exists, base unknown, path taken).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch is None:
        await gitops.run(repo, "worktree", "add", "--detach", str(path), base)
    else:
        await gitops.run(repo, "worktree", "add", "-b", branch, str(path), base)
    return Worktree(repo=repo, path=path, branch=branch)


def _make_writable_and_retry(function: Callable[..., Any], path: str, _error: BaseException) -> None:
    """Read-only files (git objects, some node_modules) block deletion on Windows until made writable."""
    Path(path).chmod(stat.S_IWRITE)
    function(path)


async def remove(worktree: Worktree, *, delete_branch: bool) -> None:
    """Remove the worktree (and its branch when asked); a directory git already lost track of is deleted too."""
    await gitops.run(worktree.repo, "worktree", "remove", "--force", str(worktree.path), check=False)
    if worktree.path.exists():
        await asyncio.to_thread(shutil.rmtree, worktree.path, onexc=_make_writable_and_retry)
    await gitops.run(worktree.repo, "worktree", "prune", check=False)
    if delete_branch and worktree.branch is not None:
        await gitops.run(worktree.repo, "branch", "-D", worktree.branch, check=False)


async def reconcile(root: Path, referenced: Iterable[Path], *, untouched_since: float) -> list[Path]:
    """Remove worktree directories under `root` that no run references (a crash or a lost record left them).

    Only directories not modified since `untouched_since` are considered: pass the moment this
    process began, so a worktree a job of this process is using right now is never taken for a leftover.
    A directory that is still a worktree is removed through its repository, and the branch it was
    on is deleted with it; anything else is only deleted.

    Returns:
        The directories removed.
    """
    kept = {path.resolve() for path in referenced}

    def stray() -> list[Path]:
        return sorted(
            path
            for path in root.glob("*/*")
            if path.is_dir() and path.resolve() not in kept and path.stat().st_mtime < untouched_since
        )

    removed = await asyncio.to_thread(stray)
    for directory in removed:
        await _discard_stray(directory)
    return removed


async def _discard_stray(directory: Path) -> None:
    toplevel = await gitops.run(directory, "rev-parse", "--show-toplevel", check=False)
    common = await gitops.run(directory, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    # A directory git no longer sees as a worktree may resolve to an enclosing repository: only delete it.
    if not toplevel.ok or not common.ok or Path(toplevel.stdout.strip()).resolve() != directory.resolve():
        await asyncio.to_thread(shutil.rmtree, directory, onexc=_make_writable_and_retry)
        return
    branch = await gitops.run(directory, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    await remove(
        Worktree(repo=Path(common.stdout.strip()), path=directory, branch=branch.stdout.strip() or None),
        delete_branch=True,
    )


@asynccontextmanager
async def temporary(repo: Path, path: Path, *, base: str) -> AsyncIterator[Worktree]:
    """A detached worktree that is removed afterwards, whatever happens inside."""
    worktree = await create(repo, path, base=base, branch=None)
    try:
        yield worktree
    finally:
        await remove(worktree, delete_branch=False)


DASHBOARD_IDENTITY = {"GIT_AUTHOR_NAME": "forge dashboard", "GIT_AUTHOR_EMAIL": "forge-dashboard@localhost"}


async def identity_env(repo: Path) -> dict[str, str]:
    """The repo's own committer identity when one is configured; otherwise the dashboard's."""
    configured = await gitops.run(repo, "config", "user.email", env=tree.minimal_env(), check=False)
    if configured.ok and configured.stdout.strip():
        return {}
    return {
        **DASHBOARD_IDENTITY,
        "GIT_COMMITTER_NAME": "forge dashboard",
        "GIT_COMMITTER_EMAIL": "forge-dashboard@localhost",
    }


async def commit_all(path: Path, message: str) -> str | None:
    """Stage everything in a dashboard worktree and commit, running the project's own commit hooks.

    Hook fixers (formatters) may rewrite files and fail the first attempt; their
    rewrites are staged and the commit is retried once. A hook that still
    fails raises with its output. A merge in progress is always concluded, even
    when its tree equals HEAD: without the merge commit the merged-in history
    never becomes an ancestor.

    Returns:
        The new commit's sha, or None when there was nothing to commit.

    Raises:
        gitops.GitError: when the commit fails twice.
    """
    env = tree.minimal_env()
    await gitops.run(path, "add", "-A", env=env)
    merging = (await gitops.run(path, "rev-parse", "--verify", "-q", "MERGE_HEAD", env=env, check=False)).ok
    nothing_staged = (await gitops.run(path, "diff", "--cached", "--quiet", env=env, check=False)).exit_code == 0
    if nothing_staged and not merging:
        return None
    env.update(await identity_env(path))
    first = await gitops.run(path, "commit", "-q", "-m", message, env=env, timeout_s=1800, check=False)
    if not first.ok:
        await gitops.run(path, "add", "-A", env=env)
        await gitops.run(path, "commit", "-q", "-m", message, env=env, timeout_s=1800)
    return (await gitops.run(path, "rev-parse", "HEAD", env=env)).stdout.strip()
