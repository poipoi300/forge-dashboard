"""Async git commands over the shared process module, with parsed results.

Reads never write to the user's repository: `GIT_OPTIONAL_LOCKS=0` keeps
`git status` from refreshing the index, and no read command takes a lock.
Every command is non-interactive (`GIT_TERMINAL_PROMPT=0`,
`GCM_INTERACTIVE=never`), so no credential prompt or GUI can appear.
"""

from __future__ import annotations

import asyncio
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


def git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    return tree.child_env({**NON_INTERACTIVE, **(extra or {})})


async def run(
    repo: Path,
    *args: str,
    timeout_s: float = 60,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    check: bool = True,
) -> tree.CompletedRun:
    """Run `git -C repo <args>`; raise `GitError` on failure unless `check` is False."""
    result = await tree.run(
        ["git", "-C", str(repo), *CONFIG, *args], repo, git_env(env), timeout_s=timeout_s, stdin=stdin
    )
    if check and not result.ok:
        reason = "timed out" if result.timed_out else (result.stderr.strip() or result.stdout.strip())
        raise GitError(f"git {args[0]} failed: {reason}")
    return result


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
    args = ["log", f"-n{limit}", f"--format={gitparse.LOG_FORMAT}", *(["--shortstat"] if stats else []), *revisions]
    result = await run(repo, *args, check=False)
    if not result.ok:
        # An empty repository has no HEAD to log; that is an empty history, not a failure.
        if "does not have any commits" in result.stderr or "unknown revision" in result.stderr:
            return []
        raise GitError(f"git log failed: {result.stderr.strip()}")
    return gitparse.parse_log(result.stdout)


async def branches(repo: Path) -> list[gitparse.Branch]:
    raw = await output(repo, "for-each-ref", "refs/heads", f"--format={gitparse.BRANCH_FORMAT}")
    return gitparse.parse_branches(raw)


async def remotes(repo: Path) -> dict[str, str]:
    return gitparse.parse_remotes(await output(repo, "remote", "-v"))


async def worktrees(repo: Path) -> list[gitparse.Worktree]:
    return gitparse.parse_worktrees(await output(repo, "worktree", "list", "--porcelain"))


async def stash_count(repo: Path) -> int:
    raw = await output(repo, "stash", "list", "--format=%gd")
    return len([line for line in raw.splitlines() if line])


async def count(repo: Path, revision_range: str) -> int:
    return int((await output(repo, "rev-list", "--count", revision_range)).strip() or 0)


async def ref_exists(repo: Path, ref: str) -> bool:
    return (await run(repo, "rev-parse", "--verify", "-q", ref, check=False)).ok


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
