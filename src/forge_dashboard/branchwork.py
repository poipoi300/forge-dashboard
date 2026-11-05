"""Work produced on a dashboard worktree branch, waiting for a one-click merge.

Bring-up to standards, adopt, delegate issue, and update dependencies all end
the same way: a branch in a worktree under the data dir, a diff, check
results, and the choice to merge (fast-forward the base branch only), keep the
branch, open a PR, or discard. This module stores those runs and lands them.
"""

from __future__ import annotations

import time
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from forge_dashboard import gitops, landing, worktrees
from forge_dashboard.core.db import Database
from forge_dashboard.core.service import ActionError
from forge_dashboard.core.ui import ui

BranchRunKind = Literal["standards", "adopt", "delegate", "update-deps"]


class BranchRunState(StrEnum):
    RUNNING = "running"
    READY = "ready"
    MERGED = "merged"
    PR_OPENED = "pr-opened"
    DISCARDED = "discarded"
    FAILED = "failed"


BRANCH_STATE_UI = ui(
    format="badge",
    enum_labels={"ready": "ready to merge", "pr-opened": "PR opened"},
    tones={
        "running": "info",
        "ready": "warn",
        "merged": "good",
        "pr-opened": "good",
        "discarded": "neutral",
        "failed": "bad",
    },
)


class CheckOutcomeBrief(BaseModel):
    check_id: str
    label: str
    outcome: str = Field(
        json_schema_extra=ui(
            format="badge", tones={"passed": "good", "failed": "bad", "error": "bad", "timeout": "warn"}
        )
    )


class Score(BaseModel):
    """Compliance checks passing out of applicable."""

    passing: int
    total: int


class BranchRun(BaseModel):
    id: int = Field(json_schema_extra=ui(hidden=True))
    project_id: str
    kind: BranchRunKind = Field(json_schema_extra=ui(format="badge"))
    title: str
    branch: str = Field(json_schema_extra=ui(format="code"))
    worktree: str = Field(json_schema_extra=ui(format="path"))
    base_branch: str = Field(json_schema_extra=ui(format="code"))
    base_sha: str = Field(json_schema_extra=ui(format="code", hidden=True))
    state: BranchRunState = Field(json_schema_extra=BRANCH_STATE_UI)
    created_at: float = Field(json_schema_extra=ui(format="relative-time"))
    updated_at: float = Field(json_schema_extra=ui(format="relative-time"))
    job_id: str | None = Field(None, json_schema_extra=ui(hidden=True))
    agent_run_ids: list[str] = Field(default_factory=list, json_schema_extra=ui(hidden=True))
    summary: str | None = None
    checks: list[CheckOutcomeBrief] = Field(default_factory=list, json_schema_extra=ui(columns=["label", "outcome"]))
    files_changed: int = 0
    diffstat: str = Field("", json_schema_extra=ui(format="code"))
    pr_url: str | None = Field(None, json_schema_extra=ui(format="url"))
    error: str | None = None
    agent_attempts: int = Field(0, json_schema_extra=ui(short_label="attempts"))
    compliance_before: Score | None = None
    compliance_after: Score | None = None
    still_failing: list[str] = Field(default_factory=list, json_schema_extra=ui(help="Compliance checks left failing."))
    deterministic_fixes: list[str] = Field(default_factory=list)
    hooks_installed: bool | None = None


async def unique_branch(repo: Path, wanted: str) -> str:
    """`wanted`, or `wanted-2`, `wanted-3`, ... when runs start within the same second."""
    candidate, suffix = wanted, 1
    while await gitops.ref_exists(repo, f"refs/heads/{candidate}"):
        suffix += 1
        candidate = f"{wanted}-{suffix}"
    return candidate


class BranchWork:
    """Persistence and landing for branch runs."""

    def __init__(self, db: Database, worktree_root: Path) -> None:
        self._db = db
        self._worktree_root = worktree_root

    async def start(
        self,
        *,
        repo: Path,
        project_id: str,
        kind: BranchRunKind,
        title: str,
        branch_prefix: str,
        base_branch: str,
        job_id: str,
    ) -> tuple[BranchRun, worktrees.Worktree]:
        """Create the worktree on a fresh branch from `base_branch` and record the run."""
        base_sha = (await gitops.output(repo, "rev-parse", f"refs/heads/{base_branch}")).strip()
        branch = await unique_branch(repo, f"{branch_prefix}/{worktrees.timestamp()}")
        path = worktrees.worktree_path(self._worktree_root, project_id, branch)
        worktree = await worktrees.create(repo, path, base=base_sha, branch=branch)
        now = time.time()
        run = BranchRun(
            id=0,
            project_id=project_id,
            kind=kind,
            title=title,
            branch=branch,
            worktree=str(path),
            base_branch=base_branch,
            base_sha=base_sha,
            state=BranchRunState.RUNNING,
            created_at=now,
            updated_at=now,
            job_id=job_id,
        )
        run_id = await self._db.execute(
            "INSERT INTO branch_runs (project_id, kind, job_id, started_at, state, record) VALUES (?,?,?,?,?,?)",
            (project_id, kind, job_id, now, run.state, run.model_dump_json()),
        )
        run = run.model_copy(update={"id": run_id})
        await self.save(run)
        return run, worktree

    async def save(self, run: BranchRun) -> BranchRun:
        stamped = run.model_copy(update={"updated_at": time.time()})
        await self._db.execute(
            "UPDATE branch_runs SET state = ?, record = ? WHERE id = ?",
            (stamped.state, stamped.model_dump_json(), run.id),
        )
        return stamped

    async def get(self, run_id: int) -> BranchRun:
        row = await self._db.fetch_one("SELECT record FROM branch_runs WHERE id = ?", (run_id,))
        if row is None:
            raise ActionError(f"no branch run {run_id}", status=404)
        return BranchRun.model_validate_json(row["record"])

    async def recent(self, project_id: str | None = None, kind: str | None = None, limit: int = 50) -> list[BranchRun]:
        clauses: list[str] = []
        params: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.fetch_all(
            f"SELECT record FROM branch_runs {where} ORDER BY started_at DESC LIMIT ?", (*params, limit)
        )
        return [BranchRun.model_validate_json(row["record"]) for row in rows]

    async def finish(self, run: BranchRun, repo: Path, *, summary: str, error: str | None = None) -> BranchRun:
        """Record the diff the run produced; a run with no changes or an error ends as failed."""
        stat = await gitops.output(repo, "diff", "--stat", f"{run.base_sha}...{run.branch}")
        names = await gitops.output(repo, "diff", "--name-only", f"{run.base_sha}...{run.branch}")
        files = len([line for line in names.splitlines() if line])
        state = BranchRunState.FAILED if error is not None or files == 0 else BranchRunState.READY
        if files == 0 and error is None:
            error = "the run produced no changes"
        return await self.save(
            run.model_copy(
                update={
                    "state": state,
                    "summary": summary,
                    "error": error,
                    "files_changed": files,
                    "diffstat": stat.strip(),
                }
            )
        )

    async def diff(self, run: BranchRun, repo: Path) -> str:
        return await gitops.diff(repo, f"{run.base_sha}...{run.branch}")

    async def merge(self, run: BranchRun, repo: Path) -> BranchRun:
        """Fast-forward the base branch to the run's branch, then remove the worktree and branch.

        Raises:
            ActionError: when the base branch moved on (not a fast-forward) or is
                checked out with local changes; the run stays ready.
        """
        if run.state is not BranchRunState.READY:
            raise ActionError(f"run is {run.state.value}, not ready to merge", status=409)
        tip = (await gitops.output(repo, "rev-parse", f"refs/heads/{run.branch}")).strip()
        result = await landing.land(repo, run.base_branch, tip)
        if result.outcome == "dirty":
            raise ActionError(
                f"{run.base_branch} is checked out with local changes at {result.checkout}; "
                "commit or stash them there, then merge again (the branch is kept)",
                status=409,
            )
        if result.outcome != "updated":
            raise ActionError(
                f"{run.base_branch} cannot fast-forward to {run.branch} ({result.outcome}); merge it by hand or "
                "discard and rerun",
                status=409,
            )
        await worktrees.remove(
            worktrees.Worktree(repo=repo, path=Path(run.worktree), branch=run.branch), delete_branch=True
        )
        return await self.save(run.model_copy(update={"state": BranchRunState.MERGED}))

    async def discard(self, run: BranchRun, repo: Path) -> BranchRun:
        """Remove the worktree and delete the branch."""
        if run.state in (BranchRunState.MERGED, BranchRunState.DISCARDED):
            raise ActionError(f"run is already {run.state.value}", status=409)
        await worktrees.remove(
            worktrees.Worktree(repo=repo, path=Path(run.worktree), branch=run.branch), delete_branch=True
        )
        return await self.save(run.model_copy(update={"state": BranchRunState.DISCARDED}))
