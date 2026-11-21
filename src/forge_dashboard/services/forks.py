"""Fork management: detection, status and conflict prediction, policies, and sync runs.

A sync run is a job, always in an isolated worktree under the data dir; it
never touches the user's working tree and never stashes their work:

1. Fetch upstream and origin; create `forge-sync/<upstream-branch>-<stamp>` from the target.
2. Merge (or rebase onto) upstream. Clean: go to 4. A merge or rebase that fails for any
   reason other than conflicts fails the run.
3. Conflicts: brief the harness, let it edit, verify (no markers, `git diff --check`,
   files parse), retry with the failure appended up to the attempt limit.
4. Run the policy's checks in the worktree; a failure parks the run.
5. Commit (recording the upstream SHA and whether an agent resolved conflicts), verify the
   result contains upstream, then fast-forward the target (directly when not checked out,
   in its checkout when clean, otherwise park as ready-to-apply), and push when the policy says so.

However a run's work ends, its record does too: an unexpected error parks the run (worktree
kept, so it can be applied, retried, or aborted), and cancelling the job aborts it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, override

from pydantic import BaseModel, Field

from forge_dashboard import gitops, landing, worktrees
from forge_dashboard.branchwork import INTERRUPTED
from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, Done, JobRef, ProjectRef, Service, action
from forge_dashboard.core.tasks import BackgroundTasks
from forge_dashboard.core.ui import ui
from forge_dashboard.domain import forks as rules
from forge_dashboard.domain.forks import PARKED, ConflictedFile, ForkPolicy, ForkRun, ForkRunState
from forge_dashboard.domain.gitparse import Commit, redact_url
from forge_dashboard.domain.harness import RunResult
from forge_dashboard.domain.scheduling import ManualSchedule
from forge_dashboard.domain.summary import ForkFacts
from forge_dashboard.process import tree
from forge_dashboard.services.agents import AgentsService, base_branch_of
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import ChangeKind
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService
from forge_dashboard.services.scheduler import SchedulerService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

UPSTREAM_REMOTE = "upstream"
LastSync = Literal["synced", "conflict", "needs-attention", "ready-to-apply", "failed", "never"]
LAST_SYNC: dict[ForkRunState, LastSync] = {
    ForkRunState.SYNCED: "synced",
    ForkRunState.UP_TO_DATE: "synced",
    ForkRunState.CONFLICT: "conflict",
    ForkRunState.NEEDS_ATTENTION: "needs-attention",
    ForkRunState.READY_TO_APPLY: "ready-to-apply",
    ForkRunState.FAILED: "failed",
}
PREVIEW_COMMITS = 50
GIT_FAILURE_LINES = 20
# Runs in these states own a worktree, so history pruning must not drop them.
HOLDS_WORKTREE = PARKED | {ForkRunState.RUNNING}

ParentLookup = Callable[[str], Awaitable[str | None]]


class UpstreamCommit(BaseModel):
    short_sha: str = Field(json_schema_extra=ui(format="code"))
    subject: str
    author: str
    committed_at: float = Field(json_schema_extra=ui(format="relative-time"))


class ChangedFile(BaseModel):
    status: str = Field(json_schema_extra=ui(format="badge"))
    path: str = Field(json_schema_extra=ui(format="path"))


class ConflictPrediction(BaseModel):
    clean: bool = Field(json_schema_extra=ui(format="badge", tones={"true": "good", "false": "bad"}))
    conflicted_files: list[str] = Field(default_factory=list)
    checked_at: float = Field(json_schema_extra=ui(format="relative-time"))


class ForkStatus(BaseModel):
    project_id: str
    is_fork: bool
    source: Literal["upstream-remote", "github", "none"] = "none"
    upstream_url: str | None = Field(None, json_schema_extra=ui(format="url"))
    upstream_branch: str | None = Field(None, json_schema_extra=ui(format="code"))
    target_branch: str | None = Field(None, json_schema_extra=ui(format="code"))
    behind: int | None = None
    ahead: int | None = None
    fetched_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    prediction: ConflictPrediction | None = None
    last_run: ForkRun | None = None
    next_sync_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    policy: ForkPolicy = Field(default_factory=ForkPolicy)
    error: str | None = None


class ForksConfig(BaseModel):
    keep_runs: int = Field(
        200,
        ge=10,
        le=5000,
        json_schema_extra=ui(
            advanced=True, help="Finished runs kept per fork; parked and running runs are never pruned."
        ),
    )


class ForksState(BaseModel):
    forks: list[ForkStatus] = Field(
        default_factory=list, json_schema_extra=ui(columns=["project_id", "upstream_branch", "behind", "ahead"])
    )


class PolicyInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    policy: ForkPolicy


class PreviewInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))


class Preview(BaseModel):
    status: ForkStatus
    commits: list[UpstreamCommit] = Field(
        json_schema_extra=ui(columns=["short_sha", "subject", "author", "committed_at"])
    )
    files: list[ChangedFile] = Field(json_schema_extra=ui(columns=["status", "path"]))


class RunInput(BaseModel):
    run_id: str = Field(json_schema_extra=ui(hidden=True))


class RetryInput(BaseModel):
    run_id: str = Field(json_schema_extra=ui(hidden=True))
    instructions: str | None = Field(None, json_schema_extra=ui(widget="textarea", placeholder="What the agent missed"))


class DiffInput(BaseModel):
    run_id: str = Field(json_schema_extra=ui(hidden=True))
    against: Literal["target", "upstream"] = Field("target", json_schema_extra=ui(widget="radio-cards"))


class RunDiff(BaseModel):
    run: ForkRun
    against: str
    diff: str = Field(json_schema_extra=ui(format="code"))


class HistoryInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    limit: int = Field(50, ge=1, le=500)


class History(BaseModel):
    runs: list[ForkRun] = Field(
        json_schema_extra=ui(columns=["state", "started_at", "duration_s", "agent_attempts", "agent_cost_usd"])
    )


class _ForkEvent(Event):
    run_id: str
    notify: bool
    detail: str | None = None

    def should_notify(self) -> bool:
        return self.notify


class ForkSynced(_ForkEvent):
    type: ClassVar[str] = "forks.synced"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "fork-success"
    upstream_sha: str | None = None

    def title(self) -> str:
        return f"Fork synced with upstream ({(self.upstream_sha or '')[:7]})"

    def severity(self) -> Severity:
        return "success"


class ForkNeedsAttention(_ForkEvent):
    type: ClassVar[str] = "forks.needs-attention"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "fork-conflict"
    state: str

    def title(self) -> str:
        return f"Fork sync parked: {self.state}" + (f" ({self.detail})" if self.detail else "")

    def severity(self) -> Severity:
        return "warning"


class ForkSyncFailed(_ForkEvent):
    type: ClassVar[str] = "forks.failed"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "fork-failure"

    def title(self) -> str:
        return f"Fork sync failed: {self.detail}"

    def severity(self) -> Severity:
        return "error"


@dataclass
class _Sync:
    """Everything one sync run is working with."""

    record: ProjectRecord
    policy: ForkPolicy
    run: ForkRun
    worktree: worktrees.Worktree | None = None

    @property
    def checkout(self) -> worktrees.Worktree:
        """The run's worktree; asking before one exists is a bug in the flow."""
        if self.worktree is None:
            raise RuntimeError(f"fork run {self.run.id} has no worktree")
        return self.worktree


@dataclass(frozen=True)
class _Round:
    """What one merge or rebase step left behind: files to resolve, or why git failed outright."""

    conflicted: list[str]
    failure: str | None = None


def _tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-GIT_FAILURE_LINES:])


class ForksService(Service[ForksConfig, ForksState], ProjectListener):
    id = "forks"
    title = "Forks"
    description = "Keeps forks in step with upstream: prediction, policies, isolated sync runs, and parked runs."
    Config = ForksConfig
    State = ForksState
    events = (ForkSynced, ForkNeedsAttention, ForkSyncFailed)

    def __init__(
        self,
        runtime: Runtime,
        projects: ProjectsService,
        git: GitService,
        jobs: JobsService,
        scheduler: SchedulerService,
        checks: ChecksService,
        agents: AgentsService,
    ) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._git = git
        self._jobs = jobs
        self._scheduler = scheduler
        self._checks = checks
        self._agents = agents
        self._status: dict[str, ForkStatus] = {}
        self._parent_lookup: ParentLookup | None = None
        self._background = BackgroundTasks()
        self._constructed_at = time.time()
        projects.listen(self)

    def connect_github(self, parent_lookup: ParentLookup) -> None:
        """Let detection ask GitHub for a repo's parent clone URL."""
        self._parent_lookup = parent_lookup

    @override
    async def start(self) -> None:
        await self._recover_interrupted()
        await self._reconcile_worktrees()

    @override
    async def stop(self) -> None:
        await self._background.cancel_all()

    async def _recover_interrupted(self) -> None:
        """Runs still `running` at startup lost their job. One that kept a worktree is parked, so it can be
        applied, retried, or aborted; one without is failed.
        """
        rows = await self.runtime.db.fetch_all("SELECT record FROM fork_runs WHERE state = ?", (ForkRunState.RUNNING,))
        now = time.time()
        for row in rows:
            run = ForkRun.model_validate_json(row["record"])
            kept = run.worktree is not None and await asyncio.to_thread(Path(run.worktree).is_dir)
            await self._save(
                run.model_copy(
                    update={
                        "state": ForkRunState.NEEDS_ATTENTION if kept else ForkRunState.FAILED,
                        "detail": INTERRUPTED,
                        "worktree": run.worktree if kept else None,
                        "ended_at": now,
                        "duration_s": now - run.started_at,
                    }
                )
            )

    async def _reconcile_worktrees(self) -> None:
        """Remove worktree directories (and their branches) that no fork run and no branch run references."""
        rows = await self.runtime.db.fetch_all(
            "SELECT json_extract(record, '$.worktree') AS worktree FROM fork_runs "
            "WHERE json_extract(record, '$.worktree') IS NOT NULL"
        )
        referenced = [Path(row["worktree"]) for row in rows]
        referenced += await self._agents.branch_work.referenced_worktrees()
        stray = await worktrees.reconcile(
            self.runtime.paths.worktrees, referenced, untouched_since=self._constructed_at
        )
        for directory in stray:
            self.runtime.log.warning("removed unreferenced worktree %s", directory)

    # --- registry hooks ---

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        if record.is_git:
            self._background.spawn(self.refresh(record), name=f"forks-refresh:{record.id}")

    @override
    async def project_removed(self, project_id: str) -> None:
        self._status.pop(project_id, None)
        self._publish_state()

    @override
    async def files_changed(self, record: ProjectRecord, kinds: frozenset[ChangeKind]) -> None:
        if "git" in kinds and record.id in self._status:
            self._background.spawn(self.refresh(record), name=f"forks-refresh:{record.id}")

    # --- status ---

    async def _policy(self, project_id: str) -> ForkPolicy:
        stored = await self.runtime.db.kv_get("fork-policy", project_id)
        return ForkPolicy.model_validate(stored) if stored is not None else ForkPolicy()

    async def _upstream_url(
        self, record: ProjectRecord
    ) -> tuple[str | None, Literal["upstream-remote", "github", "none"]]:
        remotes = await gitops.remotes(record.path)
        if UPSTREAM_REMOTE in remotes:
            return remotes[UPSTREAM_REMOTE], "upstream-remote"
        if self._parent_lookup is not None:
            parent = await self._parent_lookup(record.id)
            if parent is not None:
                return parent, "github"
        return None, "none"

    async def _upstream_branch(self, record: ProjectRecord, policy: ForkPolicy) -> str:
        if policy.upstream_branch:
            return policy.upstream_branch
        head = await gitops.run(
            record.path, "symbolic-ref", "--short", f"refs/remotes/{UPSTREAM_REMOTE}/HEAD", check=False
        )
        if head.ok and head.stdout.strip():
            return head.stdout.strip().removeprefix(f"{UPSTREAM_REMOTE}/")
        for candidate in ("main", "master"):
            if await gitops.ref_exists(record.path, f"refs/remotes/{UPSTREAM_REMOTE}/{candidate}"):
                return candidate
        return "main"

    async def _status_of(self, record: ProjectRecord, *, predict: bool) -> ForkStatus:
        """Behind/ahead and the conflict prediction from the fetched refs; changes and publishes nothing."""
        policy = await self._policy(record.id)
        url, source = await self._upstream_url(record)
        last = await self._last_run(record.id)
        entry = self._scheduler.entry(f"forks:{record.id}")
        status = ForkStatus(
            project_id=record.id,
            is_fork=source != "none",
            source=source,
            upstream_url=url,
            policy=policy,
            last_run=last,
            next_sync_at=entry.next_run_at if entry is not None else None,
        )
        if source == "upstream-remote":
            try:
                status = await self._compare(record, policy, status, predict=predict)
            except gitops.GitError as error:
                status = status.model_copy(update={"error": error.message})
        return status

    async def refresh(self, record: ProjectRecord, *, predict: bool = True) -> ForkStatus:
        """Recompute the fork's status from the fetched upstream refs and publish it."""
        status = await self._status_of(record, predict=predict)
        self._status[record.id] = status
        self._publish(record.id)
        return status

    async def _compare(
        self, record: ProjectRecord, policy: ForkPolicy, status: ForkStatus, *, predict: bool
    ) -> ForkStatus:
        upstream_branch = await self._upstream_branch(record, policy)
        target = policy.target_branch or await base_branch_of(record)
        upstream_ref = f"refs/remotes/{UPSTREAM_REMOTE}/{upstream_branch}"
        update: dict[str, object] = {
            "upstream_branch": upstream_branch,
            "target_branch": target,
            "fetched_at": await gitops.fetch_time(record.path),
        }
        if not await gitops.ref_exists(record.path, upstream_ref):
            update["error"] = f"{UPSTREAM_REMOTE}/{upstream_branch} has not been fetched yet"
            return status.model_copy(update=update)
        counts = await gitops.output(
            record.path, "rev-list", "--left-right", "--count", f"refs/heads/{target}...{upstream_ref}"
        )
        ahead, behind = (int(part) for part in counts.split())
        update |= {"ahead": ahead, "behind": behind}
        if predict and behind:
            prediction = await gitops.merge_tree(record.path, f"refs/heads/{target}", upstream_ref)
            update["prediction"] = ConflictPrediction(
                clean=prediction.clean, conflicted_files=list(prediction.conflicted_files), checked_at=time.time()
            )
        return status.model_copy(update=update)

    def _publish(self, project_id: str) -> None:
        status = self._status.get(project_id)
        if status is not None:
            last = status.last_run
            last_sync: LastSync = LAST_SYNC.get(last.state, "never") if last is not None else "never"
            self._projects.put(
                project_id,
                ForkFacts(
                    is_fork=status.is_fork,
                    upstream=status.upstream_url,
                    behind_upstream=status.behind,
                    ahead_of_upstream=status.ahead,
                    last_sync=last_sync,
                    last_sync_at=last.ended_at if last is not None else None,
                    next_sync_at=status.next_sync_at,
                ),
            )
        self._publish_state()

    def _publish_state(self) -> None:
        forks = [status for status in self._status.values() if status.is_fork]
        self.set_state(ForksState(forks=sorted(forks, key=lambda status: status.project_id)))

    # --- run records ---

    async def _save(self, run: ForkRun) -> ForkRun:
        await self.runtime.db.execute(
            "INSERT INTO fork_runs (id, project_id, job_id, started_at, ended_at, state, record) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET "
            "job_id = excluded.job_id, ended_at = excluded.ended_at, state = excluded.state, record = excluded.record",
            (run.id, run.project_id, run.job_id, run.started_at, run.ended_at, run.state, run.model_dump_json()),
        )
        if run.state not in HOLDS_WORKTREE:
            await self._prune(run.project_id)
        status = self._status.get(run.project_id)
        if status is not None:
            self._status[run.project_id] = status.model_copy(update={"last_run": run})
            self._publish(run.project_id)
        return run

    async def _prune(self, project_id: str) -> None:
        """Keep the newest `keep_runs` runs of a fork, plus every run that still owns a worktree."""
        protected = sorted(state.value for state in HOLDS_WORKTREE)
        await self.runtime.db.execute(
            "DELETE FROM fork_runs WHERE project_id = ? "
            f"AND state NOT IN ({','.join('?' * len(protected))}) "
            "AND id NOT IN (SELECT id FROM fork_runs WHERE project_id = ? ORDER BY started_at DESC LIMIT ?)",
            (project_id, *protected, project_id, self.config.keep_runs),
        )

    async def _last_run(self, project_id: str) -> ForkRun | None:
        row = await self.runtime.db.fetch_one(
            "SELECT record FROM fork_runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1", (project_id,)
        )
        return ForkRun.model_validate_json(row["record"]) if row is not None else None

    async def _run(self, run_id: str) -> ForkRun:
        row = await self.runtime.db.fetch_one("SELECT record FROM fork_runs WHERE id = ?", (run_id,))
        if row is None:
            raise ActionError(f"no fork run {run_id!r}", status=404)
        return ForkRun.model_validate_json(row["record"])

    # --- the sync flow ---

    def _checks_for(self, record: ProjectRecord, policy: ForkPolicy) -> list[str]:
        manifest_checks = record.manifest.checks if record.manifest is not None else []
        if policy.checks is not None:
            return [c.id for c in manifest_checks if c.id in policy.checks]
        return [c.id for c in manifest_checks if c.stage in ("commit", "push")]

    async def _finish(self, context: JobContext, sync: _Sync, state: ForkRunState, detail: str | None) -> JobOutcome:
        run = sync.run
        ended = time.time()
        run = await self._save(
            run.model_copy(
                update={"state": state, "detail": detail, "ended_at": ended, "duration_s": ended - run.started_at}
            )
        )
        sync.run = run
        policy = sync.policy
        if state in (ForkRunState.SYNCED, ForkRunState.UP_TO_DATE):
            self.publish(
                ForkSynced(
                    project_id=run.project_id,
                    run_id=run.id,
                    notify="success" in policy.notify,
                    detail=detail,
                    upstream_sha=run.upstream_sha,
                )
            )
            await self.refresh(sync.record, predict=False)
            return JobOutcome(summary=detail or state.value)
        if state in PARKED:
            self.publish(
                ForkNeedsAttention(
                    project_id=run.project_id,
                    run_id=run.id,
                    state=state.value,
                    detail=detail,
                    notify="conflict" in policy.notify,
                )
            )
            await self.refresh(sync.record, predict=False)
            return JobOutcome(status=JobStatus.ATTENTION, summary=f"{state.value}: {detail}" if detail else state.value)
        self.publish(
            ForkSyncFailed(project_id=run.project_id, run_id=run.id, detail=detail, notify="failure" in policy.notify)
        )
        await self.refresh(sync.record, predict=False)
        context.log(f"sync failed: {detail}")
        return JobOutcome(status=JobStatus.FAILED, summary=detail)

    @asynccontextmanager
    async def _guarded(self, context: JobContext, sync: _Sync) -> AsyncIterator[None]:
        """Whatever ends the work on a still-running sync early, its record ends with it.

        Cancelling the job aborts the run and removes its worktree and branch. Any other failure
        parks the run with the error (the worktree is kept, so the work can be inspected, applied,
        retried, or aborted), or fails it when no worktree is left. A run the work already finished stays as recorded.
        """
        try:
            yield
        except asyncio.CancelledError:
            if sync.run.state is ForkRunState.RUNNING:
                await self._cleanup(sync, delete_branch=True)
                ended = time.time()
                sync.run = await self._save(
                    sync.run.model_copy(
                        update={
                            "state": ForkRunState.ABORTED,
                            "detail": "cancelled with its job",
                            "ended_at": ended,
                            "duration_s": ended - sync.run.started_at,
                        }
                    )
                )
            raise
        except Exception as error:
            if sync.run.state is ForkRunState.RUNNING:
                kept = sync.worktree is not None and await asyncio.to_thread(sync.worktree.path.is_dir)
                if not kept:
                    await self._cleanup(sync, delete_branch=True)
                state = ForkRunState.NEEDS_ATTENTION if kept else ForkRunState.FAILED
                await self._finish(context, sync, state, worktrees.describe_failure(error))
            raise

    async def _sync(self, context: JobContext, record: ProjectRecord) -> JobOutcome:
        policy = await self._policy(record.id)
        await self._ensure_upstream_remote(record, policy)
        remotes = await gitops.remotes(record.path)
        if UPSTREAM_REMOTE not in remotes:
            raise ActionError("no upstream: set the upstream URL in the fork policy", status=409)
        context.progress(0.05, "fetching")
        for remote in (UPSTREAM_REMOTE, "origin"):
            if remote in remotes:
                fetched = await context.run(
                    ["git", "-C", str(record.path), "fetch", "--prune", remote],
                    record.path,
                    gitops.transport_env(),
                    timeout_s=600,
                )
                if not fetched.ok:
                    raise ActionError(f"git fetch {remote} failed")
        await gitops.run(record.path, "remote", "set-head", UPSTREAM_REMOTE, "--auto", check=False, timeout_s=120)
        upstream_branch = await self._upstream_branch(record, policy)
        target = policy.target_branch or await base_branch_of(record)
        upstream_ref = f"{UPSTREAM_REMOTE}/{upstream_branch}"
        upstream_sha = (await gitops.output(record.path, "rev-parse", f"refs/remotes/{upstream_ref}")).strip()
        target_sha = (await gitops.output(record.path, "rev-parse", f"refs/heads/{target}")).strip()
        run = ForkRun(
            id=f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            project_id=record.id,
            job_id=context.job_id,
            state=ForkRunState.RUNNING,
            strategy=policy.strategy,
            upstream_ref=upstream_ref,
            upstream_sha=upstream_sha,
            target_branch=target,
            target_sha=target_sha,
            started_at=time.time(),
        )
        sync = _Sync(record=record, policy=policy, run=await self._save(run))
        async with self._guarded(context, sync):
            if await gitops.is_ancestor(record.path, upstream_sha, target_sha):
                return await self._finish(
                    context, sync, ForkRunState.UP_TO_DATE, f"{target} already contains {upstream_ref}"
                )

            branch = f"forge-sync/{upstream_branch}-{worktrees.timestamp()}"
            path = worktrees.worktree_path(self.runtime.paths.worktrees, record.id, branch)
            sync.worktree = await worktrees.create(record.path, path, base=target_sha, branch=branch)
            sync.run = await self._save(sync.run.model_copy(update={"branch": branch, "worktree": str(path)}))
            context.progress(0.2, f"{policy.strategy} {upstream_ref}")
            return await self._integrate(context, sync, extra_instructions=None, step=None)

    async def _integrate(
        self, context: JobContext, sync: _Sync, *, extra_instructions: str | None, step: _Round | None
    ) -> JobOutcome:
        """Steps 2-5 on the run's worktree: merge or rebase, resolve, check, commit, land.

        `step` is the round a caller already ran; None starts the merge or rebase here (or, for a
        run that has resolved conflicts before, picks up whatever conflicts the worktree holds).
        """
        if step is None:
            if sync.run.state is ForkRunState.RUNNING and not sync.run.conflicted_files:
                step = await self._start_integration(context, sync)
            else:
                step = _Round(await self._conflicted(sync.checkout.path))
        while step.conflicted:
            sync.run = await self._save(
                sync.run.model_copy(
                    update={"conflicted_files": sorted(set(sync.run.conflicted_files) | set(step.conflicted))}
                )
            )
            if sync.policy.conflicts == "stop":
                return await self._finish(
                    context,
                    sync,
                    ForkRunState.CONFLICT,
                    f"{len(step.conflicted)} conflicted file{'' if len(step.conflicted) == 1 else 's'}",
                )
            resolved, failure = await self._resolve(context, sync, step.conflicted, extra_instructions)
            if not resolved:
                return await self._finish(context, sync, ForkRunState.NEEDS_ATTENTION, failure)
            step = await self._continue(context, sync)
        if step.failure is not None:
            return await self._fail_integration(context, sync, step.failure)
        return await self._check_commit_land(context, sync)

    async def _fail_integration(self, context: JobContext, sync: _Sync, failure: str) -> JobOutcome:
        """Git itself failed (not a conflict): nothing was merged, so the worktree and branch have no value."""
        await self._cleanup(sync, delete_branch=True)
        return await self._finish(context, sync, ForkRunState.FAILED, failure)

    async def _start_integration(self, context: JobContext, sync: _Sync) -> _Round:
        if sync.policy.strategy == "merge":
            return await self._git_step(context, sync, "merge", "--no-ff", "--no-commit", sync.run.upstream_ref)
        return await self._git_step(context, sync, "rebase", sync.run.upstream_ref)

    async def _git_step(self, context: JobContext, sync: _Sync, *args: str) -> _Round:
        """Run a merge or rebase command in the worktree; unmerged paths are conflicts, any other failure is git's."""
        path = sync.checkout.path
        env = {**gitops.NON_INTERACTIVE, **await worktrees.identity_env(path), "GIT_EDITOR": "true"}
        result = await context.run(["git", "-C", str(path), *args], path, env, timeout_s=600)
        conflicted = await self._conflicted(path)
        if conflicted or result.ok:
            return _Round(conflicted)
        return _Round([], failure=f"git {args[0]} failed: {_tail(gitops.failure_reason(result))}")

    @staticmethod
    async def _conflicted(path: Path) -> list[str]:
        raw = await gitops.output(path, "diff", "--name-only", "--diff-filter=U")
        return [line for line in raw.splitlines() if line]

    async def _continue(self, context: JobContext, sync: _Sync) -> _Round:
        """After a resolution round: a merge is done; a rebase continues and may stop on the next commit."""
        if sync.policy.strategy == "merge":
            return _Round([])
        return await self._git_step(context, sync, "rebase", "--continue")

    async def _side(self, path: Path, stage: int, file: str) -> str | None:
        result = await gitops.run(path, "show", f":{stage}:{file}", check=False)
        return result.stdout if result.ok else None

    async def _brief(self, sync: _Sync, files: list[str], failure: str | None, extra: str | None) -> str:
        path = sync.checkout.path
        conflicted = [
            ConflictedFile(
                path=file,
                merged=(path / file).read_text(encoding="utf-8", errors="replace") if (path / file).is_file() else "",
                ours=await self._side(path, 2, file),
                theirs=await self._side(path, 3, file),
            )
            for file in files
        ]
        base = (
            await gitops.run(path, "merge-base", sync.run.target_sha or "HEAD", sync.run.upstream_sha, check=False)
        ).stdout.strip()
        upstream_commits: list[Commit] = (
            await gitops.log(path, f"{base}..{sync.run.upstream_sha}", limit=200, stats=False) if base else []
        )
        fork_commits: list[Commit] = (
            await gitops.log(path, f"{base}..{sync.run.target_sha}", limit=200, stats=False) if base else []
        )
        agents_md_path = sync.record.path / "AGENTS.md"
        return rules.conflict_brief(
            strategy=sync.policy.strategy,
            upstream_ref=sync.run.upstream_ref,
            upstream_sha=sync.run.upstream_sha,
            target_branch=sync.run.target_branch,
            files=conflicted,
            upstream_commits=upstream_commits,
            fork_commits=fork_commits,
            agents_md=agents_md_path.read_text(encoding="utf-8") if agents_md_path.is_file() else None,
            previous_failure=failure,
            extra_instructions=extra,
        )

    async def _verify(self, path: Path, files: list[str]) -> str | None:
        """None when the resolution is acceptable; otherwise the problems, for the next attempt's brief."""
        problems: list[str] = []
        for file in files:
            target = path / file
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
            if not rules.is_conflict_marker_free(text):
                problems.append(f"{file}: conflict markers remain")
            parse_problem = rules.parse_problems(target, text)
            if parse_problem is not None:
                problems.append(parse_problem)
        env = tree.minimal_env()
        await gitops.run(path, "add", "--", *files, env=env)
        check = await gitops.run(path, "diff", "--cached", "--check", "--", *files, env=env, check=False)
        if check.exit_code not in (0, None) and check.stdout.strip():
            problems.append(f"git diff --check:\n{check.stdout.strip()}")
        return "\n".join(problems) or None

    async def _resolve(
        self, context: JobContext, sync: _Sync, files: list[str], extra: str | None
    ) -> tuple[bool, str | None]:
        failure: str | None = None
        for _attempt in range(sync.policy.max_agent_attempts):
            prompt = await self._brief(sync, files, failure, extra)
            agent = await self._agents.run_agent(
                context, kind="fork_sync", project_id=sync.record.id, prompt=prompt, cwd=sync.checkout.path
            )
            result = agent.result
            sync.run = await self._save(
                sync.run.model_copy(
                    update={
                        "agent_attempts": sync.run.agent_attempts + 1,
                        "agent_run_ids": [*sync.run.agent_run_ids, agent.run_id],
                        "agent_cost_usd": (sync.run.agent_cost_usd or 0) + _cost(result),
                        "agent_turns": (sync.run.agent_turns or 0) + _turns(result),
                    }
                )
            )
            if agent.outcome != "succeeded":
                failure = f"the agent run ended {agent.outcome}"
                continue
            failure = await self._verify(sync.checkout.path, files)
            if failure is None:
                context.log(f"conflicts resolved after {sync.run.agent_attempts} agent attempt(s)")
                return True, None
            context.log(f"verification failed:\n{failure}")
        return False, f"unresolved after {sync.policy.max_agent_attempts} attempts: {failure}"

    async def _check_commit_land(self, context: JobContext, sync: _Sync) -> JobOutcome:
        path = sync.checkout.path
        record = sync.record
        check_ids = self._checks_for(record, sync.policy)
        if check_ids and record.manifest is not None:
            context.progress(0.7, "running checks")
            checks = [c for c in record.manifest.checks if c.id in check_ids]
            results = await self._checks.run_checks_in(
                context, record, checks, path, record_results=False, on_head=True, sha=None, dirty=False
            )
            sync.run = await self._save(sync.run.model_copy(update={"checks": [(c.id, o) for c, o in results]}))
            failing = [c.label for c, o in results if o != "passed"]
            if failing:
                return await self._finish(
                    context, sync, ForkRunState.NEEDS_ATTENTION, f"checks failing: {', '.join(failing)}"
                )
        if sync.policy.strategy == "merge":
            remotes = await gitops.remotes(record.path)
            message = rules.sync_commit_message(
                upstream_ref=sync.run.upstream_ref,
                upstream_url=remotes.get(UPSTREAM_REMOTE),
                upstream_sha=sync.run.upstream_sha,
                agent_attempts=sync.run.agent_attempts,
                agent_run_ids=sync.run.agent_run_ids,
            )
            await worktrees.commit_all(path, message)
        else:
            note = rules.sync_note(upstream_sha=sync.run.upstream_sha, agent_attempts=sync.run.agent_attempts)
            await gitops.run(
                path,
                "notes",
                "--ref=forge-sync",
                "add",
                "-f",
                "-m",
                note,
                "HEAD",
                env=tree.minimal_env(await worktrees.identity_env(path)),
            )
        result_sha = (await gitops.output(path, "rev-parse", "HEAD")).strip()
        sync.run = await self._save(sync.run.model_copy(update={"result_sha": result_sha}))
        return await self._land(context, sync)

    async def _land(self, context: JobContext, sync: _Sync) -> JobOutcome:
        record = sync.record
        run = sync.run
        result_sha = run.result_sha
        assert result_sha is not None
        if not await gitops.is_ancestor(record.path, run.upstream_sha, result_sha):
            detail = (
                f"the result {result_sha[:12]} does not contain upstream {run.upstream_sha[:12]}; nothing was landed"
            )
            return await self._finish(context, sync, ForkRunState.NEEDS_ATTENTION, detail)
        context.progress(0.9, f"updating {run.target_branch}")
        landed = await landing.land(
            record.path, run.target_branch, result_sha, rewrite=sync.policy.strategy == "rebase"
        )
        if landed.outcome == "dirty":
            detail = f"{run.target_branch} is checked out with local changes at {landed.checkout}"
            return await self._finish(context, sync, ForkRunState.READY_TO_APPLY, detail)
        if landed.outcome == "checked-out":
            detail = (
                f"{run.target_branch} is checked out at {landed.checkout} and a rebase rewrites it; "
                f"check out another branch there, then apply"
            )
            return await self._finish(context, sync, ForkRunState.READY_TO_APPLY, detail)
        if landed.outcome == "refused" and landed.checkout is not None:
            detail = f"git refused to update {run.target_branch} in {landed.checkout}: {_tail(landed.detail or '')}"
            return await self._finish(context, sync, ForkRunState.READY_TO_APPLY, detail)
        if landed.outcome != "updated":
            detail = f"{run.target_branch} moved during the sync ({landed.outcome})"
            if landed.detail:
                detail += f": {_tail(landed.detail)}"
            return await self._finish(context, sync, ForkRunState.NEEDS_ATTENTION, detail)
        if sync.policy.auto_push and "origin" in await gitops.remotes(record.path):
            lease = ["--force-with-lease"] if sync.policy.strategy == "rebase" else []
            pushed = await context.run(
                ["git", "-C", str(record.path), "push", *lease, "origin", run.target_branch],
                record.path,
                gitops.transport_env(),
                timeout_s=600,
            )
            sync.run = await self._save(sync.run.model_copy(update={"pushed": pushed.ok}))
            if not pushed.ok:
                await self._cleanup(sync, delete_branch=True)
                return await self._finish(context, sync, ForkRunState.FAILED, "synced locally, but the push failed")
        await self._cleanup(sync, delete_branch=True)
        attempts = sync.run.agent_attempts
        how = f"agent resolved conflicts in {attempts} attempt(s)" if attempts else "no conflicts"
        return await self._finish(context, sync, ForkRunState.SYNCED, f"{run.target_branch} updated; {how}")

    async def _cleanup(self, sync: _Sync, *, delete_branch: bool) -> None:
        if sync.worktree is not None:
            await worktrees.remove(sync.worktree, delete_branch=delete_branch)
            sync.worktree = None
            sync.run = await self._save(sync.run.model_copy(update={"worktree": None}))

    async def _ensure_upstream_remote(self, record: ProjectRecord, policy: ForkPolicy) -> None:
        """Point the `upstream` remote at the policy's URL, or at GitHub's parent when nothing is configured."""
        remotes = await gitops.remotes(record.path)
        wanted = policy.upstream_url
        if wanted is None and UPSTREAM_REMOTE not in remotes and self._parent_lookup is not None:
            wanted = await self._parent_lookup(record.id)
        if wanted is None or remotes.get(UPSTREAM_REMOTE) == redact_url(wanted):
            return
        verb = "set-url" if UPSTREAM_REMOTE in remotes else "add"
        await gitops.run(record.path, "remote", verb, "--", UPSTREAM_REMOTE, wanted)

    # --- actions ---

    def _job(
        self, record: ProjectRecord, title: str, work: Callable[[JobContext], Awaitable[JobOutcome]]
    ) -> Awaitable[JobRef]:
        return self._jobs.submit(
            kind="forks.sync", title=title, work=work, project_id=record.id, resources=[f"repo:{record.id}"]
        )

    @action(title="Sync fork", long_running=True)
    async def sync(self, request: ProjectRef) -> JobRef:
        """Bring the target branch up to date with upstream in an isolated worktree."""
        record = self._projects.record(request.project_id)
        if not record.is_git:
            raise ActionError(f"{record.path} is not a git repository", status=409)

        async def work(context: JobContext) -> JobOutcome:
            return await self._sync(context, record)

        return await self._job(record, f"Sync fork: {record.id}", work)

    @action(title="Fork policy")
    async def set_policy(self, request: PolicyInput) -> ForkStatus:
        """Save the fork's policy; setting an upstream URL adds the `upstream` remote, and a schedule queues syncs."""
        record = self._projects.record(request.project_id)
        await self.runtime.db.kv_put("fork-policy", record.id, request.policy.model_dump(mode="json"))
        if request.policy.upstream_url:
            await self._ensure_upstream_remote(record, request.policy)
        entry_id = f"forks:{record.id}"
        if isinstance(request.policy.schedule, ManualSchedule):
            await self._scheduler.remove(entry_id)
        else:
            await self._scheduler.upsert(
                entry_id=entry_id,
                owner=self.id,
                title=f"Sync fork: {record.id}",
                project_id=record.id,
                target_service=self.id,
                target_action="sync",
                payload={"project_id": record.id},
                schedule=request.policy.schedule,
            )
        return await self.refresh(record)

    @action(title="Fork status and preview", query=True)
    async def preview(self, request: PreviewInput) -> Preview:
        """Behind/ahead, the commits to pull, files changed, and the predicted conflicts, from the refs already fetched.

        Reads only: it fetches nothing and changes no service state, so a view that follows the
        service's revisions can call it freely. "Refresh fork status" is the action that fetches.
        """
        record = self._projects.record(request.project_id)
        status = await self._status_of(record, predict=True)
        if status.upstream_branch is None or status.target_branch is None or status.error:
            return Preview(status=status, commits=[], files=[])
        upstream_ref = f"refs/remotes/{UPSTREAM_REMOTE}/{status.upstream_branch}"
        target = f"refs/heads/{status.target_branch}"
        commits = await gitops.log(record.path, f"{target}..{upstream_ref}", limit=PREVIEW_COMMITS, stats=False)
        names = await gitops.output(record.path, "diff", "--name-status", f"{target}...{upstream_ref}")
        files = [
            ChangedFile(status=line.split("\t")[0], path=line.split("\t")[-1]) for line in names.splitlines() if line
        ]
        return Preview(
            status=status,
            commits=[
                UpstreamCommit(short_sha=c.short_sha, subject=c.subject, author=c.author, committed_at=c.committed_at)
                for c in commits
            ],
            files=files,
        )

    @action(title="Sync history", query=True)
    async def history(self, request: HistoryInput) -> History:
        """Every run's outcome, duration, and agent cost and turns."""
        rows = await self.runtime.db.fetch_all(
            "SELECT record FROM fork_runs WHERE project_id = ? ORDER BY started_at DESC LIMIT ?",
            (request.project_id, request.limit),
        )
        return History(runs=[ForkRun.model_validate_json(row["record"]) for row in rows])

    @action(title="Run diff", query=True)
    async def run_diff(self, request: DiffInput) -> RunDiff:
        """A parked run's worktree against the target branch or against upstream."""
        run = await self._run(request.run_id)
        if run.worktree is None or not Path(run.worktree).is_dir():
            raise ActionError("this run has no worktree anymore", status=409)
        base = run.target_sha if request.against == "target" else run.upstream_sha
        if base is None:
            raise ActionError("the run did not record that base", status=409)
        return RunDiff(run=run, against=request.against, diff=await gitops.diff(Path(run.worktree), base))

    async def _parked(self, run_id: str) -> _Sync:
        """The run as it is now, refused unless it is parked with a worktree to act on."""
        run = await self._run(run_id)
        if run.state not in PARKED:
            raise ActionError(f"run is {run.state.value}, not parked", status=409)
        if run.worktree is None or run.branch is None:
            raise ActionError("the run has no worktree", status=409)
        record = self._projects.record(run.project_id)
        worktree = worktrees.Worktree(repo=record.path, path=Path(run.worktree), branch=run.branch)
        return _Sync(record=record, policy=await self._policy(record.id), run=run, worktree=worktree)

    async def _resume(self, context: JobContext, run_id: str) -> _Sync:
        """In the job: re-read the run (another job may have settled it while this one queued), then mark it running."""
        sync = await self._parked(run_id)
        sync.run = await self._save(
            sync.run.model_copy(
                update={"state": ForkRunState.RUNNING, "job_id": context.job_id, "ended_at": None, "duration_s": None}
            )
        )
        return sync

    @action(title="Apply", long_running=True)
    async def apply(self, request: RunInput) -> JobRef:
        """Land a parked run on its target (after you finished it by hand, or the checkout became clean)."""
        record = (await self._parked(request.run_id)).record

        async def work(context: JobContext) -> JobOutcome:
            sync = await self._resume(context, request.run_id)
            async with self._guarded(context, sync):
                path = sync.checkout.path
                present = [file for file in sync.run.conflicted_files if (path / file).is_file()]
                leftover = [
                    file
                    for file in present
                    if not rules.is_conflict_marker_free((path / file).read_text(encoding="utf-8", errors="replace"))
                ]
                if leftover:
                    return await self._finish(
                        context, sync, ForkRunState.CONFLICT, f"markers remain in {', '.join(leftover)}"
                    )
                if sync.run.result_sha is None:
                    if present:
                        await gitops.run(path, "add", "--", *present, env=tree.minimal_env())
                    step = await self._continue(context, sync)
                    if step.failure is not None:
                        return await self._fail_integration(context, sync, step.failure)
                    if step.conflicted:
                        return await self._finish(
                            context, sync, ForkRunState.CONFLICT, "the rebase stopped on more conflicts"
                        )
                    return await self._check_commit_land(context, sync)
                if (await gitops.status(path)).dirty:
                    await worktrees.commit_all(path, f"Finish fork sync of {sync.run.upstream_ref}")
                    head = (await gitops.output(path, "rev-parse", "HEAD")).strip()
                    sync.run = await self._save(sync.run.model_copy(update={"result_sha": head}))
                return await self._land(context, sync)

        return await self._job(record, f"Apply fork sync: {record.id}", work)

    @action(title="Retry agent", long_running=True)
    async def retry_agent(self, request: RetryInput) -> JobRef:
        """Run the agent again on a parked run, with your extra instructions."""
        record = (await self._parked(request.run_id)).record

        async def work(context: JobContext) -> JobOutcome:
            sync = await self._resume(context, request.run_id)
            async with self._guarded(context, sync):
                sync.policy = sync.policy.model_copy(update={"conflicts": "agent"})
                path = sync.checkout.path
                files = await self._conflicted(path) or [f for f in sync.run.conflicted_files if (path / f).is_file()]
                resolved, failure = await self._resolve(context, sync, files, request.instructions)
                if not resolved:
                    return await self._finish(context, sync, ForkRunState.NEEDS_ATTENTION, failure)
                step = await self._continue(context, sync)
                return await self._integrate(context, sync, extra_instructions=request.instructions, step=step)

        return await self._job(record, f"Retry fork sync: {record.id}", work)

    @action(title="Abort", danger=True, confirm=True)
    async def abort(self, request: RunInput) -> Done:
        """Remove the run's worktree and branch; the target is untouched."""
        sync = await self._parked(request.run_id)
        path = sync.checkout.path
        if path.is_dir():
            await gitops.run(path, "merge", "--abort", check=False)
            await gitops.run(path, "rebase", "--abort", check=False)
        await self._cleanup(sync, delete_branch=True)
        await self._save(sync.run.model_copy(update={"state": ForkRunState.ABORTED, "ended_at": time.time()}))
        return Done(message="aborted")

    @action(title="Refresh fork status")
    async def refresh_status(self, request: ProjectRef) -> ForkStatus:
        """Fetch upstream, then recompute behind/ahead and the conflict prediction."""
        record = self._projects.record(request.project_id)
        if UPSTREAM_REMOTE in await gitops.remotes(record.path):
            await gitops.run(record.path, "fetch", "--prune", UPSTREAM_REMOTE, timeout_s=600)
        return await self.refresh(record)


def _cost(result: RunResult | None) -> float:
    return result.cost_usd if result is not None and result.cost_usd is not None else 0.0


def _turns(result: RunResult | None) -> int:
    return result.turns if result is not None and result.turns is not None else 0
