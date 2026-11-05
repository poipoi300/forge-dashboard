"""Isolated git worktrees under the dashboard's data dir, where every automated write happens.

A worktree shares the repository's object store and refs but has its own
checkout, so syncs, bring-ups, and HEAD-only check runs never touch the
user's working tree and never need to stash anything.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from forge_dashboard import gitops


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


async def remove(worktree: Worktree, *, delete_branch: bool) -> None:
    """Remove the worktree (and its branch when asked); a directory git already lost track of is deleted too."""
    await gitops.run(worktree.repo, "worktree", "remove", "--force", str(worktree.path), check=False)
    if worktree.path.exists():
        shutil.rmtree(worktree.path)
    await gitops.run(worktree.repo, "worktree", "prune", check=False)
    if delete_branch and worktree.branch is not None:
        await gitops.run(worktree.repo, "branch", "-D", worktree.branch, check=False)


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
    configured = await gitops.run(repo, "config", "user.email", check=False)
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
    fails raises with its output.

    Returns:
        The new commit's sha, or None when there was nothing to commit.

    Raises:
        gitops.GitError: when the commit fails twice.
    """
    await gitops.run(path, "add", "-A")
    if (await gitops.run(path, "diff", "--cached", "--quiet", check=False)).exit_code == 0:
        return None
    env = await identity_env(path)
    first = await gitops.run(path, "commit", "-q", "-m", message, env=env, timeout_s=1800, check=False)
    if not first.ok:
        await gitops.run(path, "add", "-A")
        await gitops.run(path, "commit", "-q", "-m", message, env=env, timeout_s=1800)
    return (await gitops.output(path, "rev-parse", "HEAD")).strip()
