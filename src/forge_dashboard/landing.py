"""Landing work from a dashboard worktree onto a user branch, only ever by fast-forward.

The target branch is updated with a compare-and-swap ref update when no
worktree has it checked out. When it is checked out in a clean worktree, the
update runs there as `git merge --ff-only`, so the checkout follows. When that
checkout has local changes, nothing is touched: the caller parks the work as
ready to apply.

Rebased fork syncs replace history by design; for them (`rewrite=True`) the
clean-checkout path uses `git reset --keep`, which refuses to lose local
changes, and the ref update still checks the old value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forge_dashboard import gitops

LandingOutcome = Literal["updated", "dirty", "not-fast-forward", "missing-branch"]


@dataclass(frozen=True)
class Landing:
    outcome: LandingOutcome
    checkout: Path | None
    previous_sha: str | None


async def checkout_of(repo: Path, branch: str) -> Path | None:
    """The worktree that has `branch` checked out, if any."""
    for worktree in await gitops.worktrees(repo):
        if worktree.branch == branch and not worktree.bare:
            return Path(worktree.path)
    return None


async def land(repo: Path, branch: str, new_sha: str, *, rewrite: bool = False) -> Landing:
    """Move `branch` to `new_sha` without ever touching a dirty checkout."""
    old = (await gitops.run(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)).stdout.strip()
    if not old:
        return Landing("missing-branch", None, None)
    if not rewrite and not await gitops.is_ancestor(repo, old, new_sha):
        return Landing("not-fast-forward", None, old)
    checkout = await checkout_of(repo, branch)
    if checkout is None:
        await gitops.run(repo, "update-ref", f"refs/heads/{branch}", new_sha, old)
        return Landing("updated", None, old)
    status = await gitops.status(checkout)
    # Untracked files do not make a checkout dirty here: git itself refuses a
    # fast-forward that would overwrite one, and that refusal parks the work.
    if any(entry.kind != "untracked" for entry in status.entries):
        return Landing("dirty", checkout, old)
    command = ("reset", "--keep", new_sha) if rewrite else ("merge", "--ff-only", new_sha)
    result = await gitops.run(checkout, *command, check=False)
    if not result.ok:
        return Landing("dirty", checkout, old)
    return Landing("updated", checkout, old)
