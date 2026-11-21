"""Async git commands over the shared process module, with parsed results.

Reads never write to the user's repository: `GIT_OPTIONAL_LOCKS=0` keeps
`git status` from refreshing the index, and no read command takes a lock.
Every command is non-interactive (`GIT_TERMINAL_PROMPT=0`,
`GCM_INTERACTIVE=never`), so no credential prompt or GUI can appear.

Text that came from an API request or a project file is never an option:
revisions follow `--end-of-options`, positional values follow `--`, remotes
are checked against `git remote`, and branches against `refs/heads`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from forge_dashboard.core.service import ActionError
from forge_dashboard.domain import gitparse
from forge_dashboard.process import tree

NON_INTERACTIVE = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GCM_GUI_PROMPT": "false",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
}
CONFIG = ("-c", "core.quotepath=off", "-c", "color.ui=never", "-c", "advice.detachedHead=false")
MAX_DIFF_BYTES = 2_000_000


class GitError(ActionError):
    """A git command failed; the message carries git's own stderr."""


def transport_env() -> dict[str, str]:
    """Extra job settings for Git network operations using a local SSH agent."""
    socket = os.environ.get("SSH_AUTH_SOCK")
    return {**NON_INTERACTIVE, "SSH_AUTH_SOCK": socket} if socket else dict(NON_INTERACTIVE)


async def run(
    repo: Path,
    *args: str,
    timeout_s: float = 60,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    check: bool = True,
) -> tree.CompletedRun:
    """Run `git -C repo <args>`; raise `GitError` on failure unless `check` is False.

    Pass a complete base environment via `env` to override inheritance.

    Raises:
        GitError: when `repo` is not a directory, or git fails and `check` is set.
    """
    if not repo.is_dir():
        raise GitError(f"{repo} is not a directory")
    git_env = {**(tree.child_env() if env is None else env), **NON_INTERACTIVE}
    result = await tree.run(["git", "-C", str(repo), *CONFIG, *args], repo, git_env, timeout_s=timeout_s, stdin=stdin)
    if check and not result.ok:
        raise GitError(f"git {args[0]} failed: {failure_reason(result)}")
    return result


def failure_reason(result: tree.CompletedRun) -> str:
    """Why a command failed, in git's own words: stderr, else stdout (merged streams), else the timeout."""
    return "timed out" if result.timed_out else (result.stderr.strip() or result.stdout.strip())


async def output(repo: Path, *args: str, timeout_s: float = 60) -> str:
    return (await run(repo, *args, timeout_s=timeout_s)).stdout


async def is_repo(path: Path) -> bool:
    result = await run(path, "rev-parse", "--is-inside-work-tree", check=False)
    return result.ok and result.stdout.strip() == "true"


async def status(repo: Path) -> gitparse.Status:
    raw = await output(repo, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
    return gitparse.parse_status_v2(raw)


async def head_sha(repo: Path) -> str | None:
    result = await run(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return result.stdout.strip() or None


async def log(repo: Path, *revisions: str, limit: int = 30, stats: bool = True) -> list[gitparse.Commit]:
    """Commits reachable from `revisions` (HEAD when none); an unborn branch has an empty history.

    Raises:
        GitError: when a revision does not exist.
    """
    if not revisions and await head_sha(repo) is None:
        return []
    args = ["log", f"-n{limit}", f"--format={gitparse.LOG_FORMAT}", *(["--shortstat"] if stats else [])]
    return gitparse.parse_log(await output(repo, *args, "--end-of-options", *revisions, "--"))


async def branches(repo: Path) -> list[gitparse.Branch]:
    raw = await output(repo, "for-each-ref", "refs/heads", f"--format={gitparse.BRANCH_FORMAT}")
    return gitparse.parse_branches(raw)


async def remotes(repo: Path) -> dict[str, str]:
    """Remote name -> fetch URL, userinfo removed: git commands address a remote by name, never by this URL."""
    return gitparse.parse_remotes(await output(repo, "remote", "-v"))


async def require_remote(repo: Path, name: str) -> str:
    """`name`, once it is confirmed to be one of the repository's remotes."""
    if name not in (await output(repo, "remote")).splitlines():
        raise GitError(f"{name!r} is not a remote of this repository")
    return name


async def require_local_branch(repo: Path, name: str) -> str:
    """`name`, once it is confirmed to be an existing local branch."""
    if not await ref_exists(repo, f"refs/heads/{name}"):
        raise GitError(f"{name!r} is not a local branch of this repository")
    return name


async def worktrees(repo: Path) -> list[gitparse.Worktree]:
    return gitparse.parse_worktrees(await output(repo, "worktree", "list", "--porcelain"))


async def stash_count(repo: Path) -> int:
    raw = await output(repo, "stash", "list", "--format=%gd")
    return len([line for line in raw.splitlines() if line])


async def count(repo: Path, revision_range: str) -> int:
    return int((await output(repo, "rev-list", "--count", "--end-of-options", revision_range)).strip() or 0)


async def ref_exists(repo: Path, ref: str) -> bool:
    return (await run(repo, "rev-parse", "--verify", "-q", "--end-of-options", ref, check=False)).ok


async def verified_commit(repo: Path, revision: str) -> str:
    """The commit `revision` names, resolved by git, so callers pass the sha on instead of the text they were given.

    Raises:
        GitError: when `revision` is not a commit of this repository.
    """
    result = await run(repo, "rev-parse", "--verify", "-q", "--end-of-options", f"{revision}^{{commit}}", check=False)
    if not result.ok or not result.stdout.strip():
        raise GitError(f"{revision!r} is not a commit of this repository")
    return result.stdout.strip()


async def default_branch(repo: Path, *, declared: str | None) -> str:
    """The branch that syncs and bring-ups start from.

    The manifest's own `declared` name wins. Without one: the branch origin's
    HEAD points at, else the checked-out branch, else `main`; the first that
    exists as a local branch.

    Raises:
        GitError: when none of them exists, so the manifest must name the branch.
    """
    if declared is not None:
        return declared
    origin_head = await run(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    checked_out = await run(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    candidates = [
        origin_head.stdout.strip().removeprefix("origin/") if origin_head.ok else "",
        checked_out.stdout.strip() if checked_out.ok else "",
        "main",
    ]
    for candidate in candidates:
        if candidate and await ref_exists(repo, f"refs/heads/{candidate}"):
            return candidate
    raise GitError(f"cannot tell which branch of {repo} is the default; set default_branch in forge.project.toml")


async def fetch_time(repo: Path) -> float | None:
    """When the checkout last fetched, from its own FETCH_HEAD (linked worktrees keep theirs apart); None before any."""
    location = (await output(repo, "rev-parse", "--git-path", "FETCH_HEAD")).strip()
    try:
        return await asyncio.to_thread(lambda: (repo / location).stat().st_mtime)
    except FileNotFoundError:
        return None


async def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (await run(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)).exit_code == 0


async def merge_tree(repo: Path, ours: str, theirs: str) -> gitparse.MergeTreeResult:
    """Predict a merge without touching any working tree."""
    result = await run(repo, "merge-tree", "--write-tree", "--name-only", "--messages", ours, theirs, check=False)
    if result.exit_code not in (0, 1):
        raise GitError(f"git merge-tree failed: {result.stderr.strip()}")
    return gitparse.parse_merge_tree(result.stdout, result.exit_code or 0)


async def diff(repo: Path, *args: str) -> str:
    """A unified diff (exit 1 from `--no-index` means "differs", not failure), truncated past a size cap."""
    result = await run(repo, "diff", "--no-color", "--no-ext-diff", *args, check=False)
    if result.exit_code not in (0, 1):
        raise GitError(f"git diff failed: {result.stderr.strip()}")
    text = result.stdout
    if len(text) > MAX_DIFF_BYTES:
        return text[:MAX_DIFF_BYTES] + "\n[diff truncated]\n"
    return text


async def newest_change_mtime(repo: Path, paths: Iterable[str]) -> float | None:
    """Newest modification time among the given repo-relative paths that still exist."""

    def newest() -> float | None:
        stamps: list[float] = []
        for relative in paths:
            try:
                stamps.append((repo / relative).stat().st_mtime)
            except OSError:
                continue
        return max(stamps, default=None)

    return await asyncio.to_thread(newest)
