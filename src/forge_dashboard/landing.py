"""Landing work from a dashboard worktree onto a user branch, only ever by fast-forward.

The target branch is updated with a compare-and-swap ref update when no
worktree has it checked out. When it is checked out in a clean worktree, the
update runs there as `git merge --ff-only`, so the checkout follows. When that
checkout has local changes, nothing is touched: the caller parks the work as
ready to apply.

Rebased fork syncs replace history by design (`rewrite=True`). A rewrite never
lands into a checked-out branch, clean or not: moving it would reset the
user's working tree, so it waits until the branch is checked out nowhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forge_dashboard import gitops

LandingOutcome = Literal["updated", "dirty", "checked-out", "refused", "not-fast-forward", "missing-branch"]


@dataclass(frozen=True)
class Landing:
    outcome: LandingOutcome
    checkout: Path | None
    previous_sha: str | None
    detail: str | None = None
    """Git's own message when it refused the update (outcome `refused`)."""


async def checkout_of(repo: Path, branch: str) -> Path | None:
    """The worktree that has `branch` checked out, if any."""
    for worktree in await gitops.worktrees(repo):
        if worktree.branch == branch and not worktree.bare:
            return Path(worktree.path)
    return None


async def land(repo: Path, branch: str, new_sha: str, *, rewrite: bool = False) -> Landing:
    """Move `branch` to `new_sha` without ever touching a dirty checkout or resetting a clean one."""
    old = (await gitops.run(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)).stdout.strip()
    if not old:
        return Landing("missing-branch", None, None)
    if not rewrite and not await gitops.is_ancestor(repo, old, new_sha):
        return Landing("not-fast-forward", None, old)
    checkout = await checkout_of(repo, branch)
    if checkout is None:
        updated = await gitops.run(repo, "update-ref", f"refs/heads/{branch}", new_sha, old, check=False)
        if not updated.ok:
            return Landing("refused", None, old, gitops.failure_reason(updated))
        return Landing("updated", None, old)
    if rewrite:
        return Landing("checked-out", checkout, old)
    status = await gitops.status(checkout)
    # Untracked files do not make a checkout dirty here: git itself refuses a
    # fast-forward that would overwrite one, and that refusal parks the work.
    if any(entry.kind != "untracked" for entry in status.entries):
        return Landing("dirty", checkout, old)
    merged = await gitops.run(checkout, "merge", "--ff-only", new_sha, check=False)
    if not merged.ok:
        return Landing("refused", checkout, old, gitops.failure_reason(merged))
    return Landing("updated", checkout, old)
