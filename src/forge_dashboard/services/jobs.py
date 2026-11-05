"""Long-running work: queueing, resource limits, logs, cancellation, history.

Any action that takes more than a moment submits a job and returns a
`JobRef`. A job names the resources it needs (`agent`, `checks`,
`repo:<project>`); it stays queued until it holds all of them, which is how
"one agent run at a time" and "never two checks in one repo" are enforced.
Output streams to a log file and to clients as batched `job.log` events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, TextIO, override

from pydantic import BaseModel, Field, JsonValue

from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, Done, JobRef, Service, action
from forge_dashboard.core.ui import ui
from forge_dashboard.process import tree

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

LOG_BATCH_S = 0.1
RECENT_JOBS = 150


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ATTENTION = "attention"


JOB_STATUS_UI = ui(
    format="badge",
    enum_labels={
        "queued": "queued",
        "running": "running",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
        "attention": "needs attention",
    },
    tones={
        "queued": "neutral",
        "running": "info",
        "succeeded": "good",
        "failed": "bad",
        "cancelled": "neutral",
        "attention": "warn",
    },
)
FINISHED = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.ATTENTION})


class JobSummary(BaseModel):
    id: str = Field(json_schema_extra=ui(hidden=True))
    kind: str = Field(json_schema_extra=ui(format="code", short_label="kind"))
    title: str
    project_id: str | None = Field(None, json_schema_extra=ui(short_label="project"))
    status: JobStatus = Field(json_schema_extra=JOB_STATUS_UI)
    created_at: float = Field(json_schema_extra=ui(format="relative-time", short_label="queued"))
    started_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="started"))
    ended_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="ended"))
    progress: float | None = Field(None, json_schema_extra=ui(format="progress"))
    progress_label: str | None = Field(None, json_schema_extra=ui(hidden=True))
    summary: str | None = None
    error: str | None = None
    waiting_for: str | None = Field(None, json_schema_extra=ui(help="Resource the job is queued on."))


class JobsConfig(BaseModel):
    agent_concurrency: int = Field(
        1, ge=1, le=4, json_schema_extra=ui(widget="slider", min=1, max=4, step=1, help="Agent runs at once.")
    )
    check_concurrency: int = Field(
        2, ge=1, le=8, json_schema_extra=ui(widget="slider", min=1, max=8, step=1, help="Check runs at once.")
    )


class JobsState(BaseModel):
    jobs: list[JobSummary] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["status", "title", "kind", "project_id", "created_at", "ended_at", "summary"]),
    )
    running: int = 0
    queued: int = 0


class JobIdInput(BaseModel):
    job_id: str = Field(json_schema_extra=ui(hidden=True))


class JobLogInput(BaseModel):
    job_id: str
    offset: int = Field(0, ge=0)
    limit: int = Field(5000, ge=1, le=50000)


class JobLogChunk(BaseModel):
    job_id: str
    offset: int
    next_offset: int
    lines: list[str]
    done: bool


class JobDetail(BaseModel):
    job: JobSummary
    result: dict[str, JsonValue] | None


class JobListInput(BaseModel):
    project_id: str | None = None
    kind: str | None = None
    limit: int = Field(50, ge=1, le=500)


class JobList(BaseModel):
    jobs: list[JobSummary]


class JobLog(Event):
    type: ClassVar[str] = "job.log"
    job_id: str
    offset: int
    lines: list[str]


class JobFinished(Event):
    type: ClassVar[str] = "job.finished"
    persist: ClassVar[bool] = True
    job_id: str
    kind: str
    job_title: str
    status: JobStatus
    summary: str | None

    def title(self) -> str:
        return f"{self.job_title}: {self.summary or self.status.value}"

    def severity(self) -> Severity:
        severities: dict[JobStatus, Severity] = {
            JobStatus.SUCCEEDED: "success",
            JobStatus.FAILED: "error",
            JobStatus.ATTENTION: "warning",
        }
        return severities.get(self.status, "info")


@dataclass
class JobOutcome:
    """What a job's work function returns."""

    status: JobStatus = JobStatus.SUCCEEDED
    summary: str | None = None
    result: BaseModel | None = None


class JobContext:
    """Handed to a job's work function: logging, progress, and subprocesses that stream into the log."""

    def __init__(self, service: JobsService, job: _Job) -> None:
        self._service = service
        self._job = job

    @property
    def job_id(self) -> str:
        return self._job.summary.id

    @property
    def log_path(self) -> Path:
        return self._job.log_path

    def log(self, line: str) -> None:
        self._service._append(self._job, line)

    def progress(self, fraction: float | None, label: str | None = None) -> None:
        self._job.summary = self._job.summary.model_copy(update={"progress": fraction, "progress_label": label})
        self._service._refresh_state()

    async def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        *,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
        echo: bool = True,
    ) -> tree.CompletedRun:
        """Run a command, streaming merged output into the job log."""
        if echo:
            self.log(f"$ {' '.join(argv)}")
        result = await tree.stream(argv, cwd, tree.child_env(env), self.log, timeout_s=timeout_s, stdin=stdin)
        if result.timed_out:
            self.log(f"timed out after {timeout_s:.0f}s")
        return result

    @asynccontextmanager
    async def hold(self, resource: str) -> AsyncIterator[None]:
        """Acquire a resource mid-job, e.g. an agent slot only when a sync actually needs one."""
        semaphore = self._service._semaphore(resource)
        if semaphore.locked():
            self.log(f"waiting for {resource}")
            self._service._update(self._job, waiting_for=resource)
        async with semaphore:
            self._service._update(self._job, waiting_for=None)
            yield


WorkFn = Callable[[JobContext], Awaitable[JobOutcome]]


@dataclass
class _Job:
    summary: JobSummary
    resources: tuple[str, ...]
    log_path: Path
    work: WorkFn
    task: asyncio.Task[None] | None = None
    log_file: TextIO | None = None
    line_count: int = 0
    pending: list[str] = field(default_factory=list)
    pending_offset: int = 0
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class JobsService(Service[JobsConfig, JobsState]):
    id = "jobs"
    title = "Jobs"
    description = "Long-running work with live logs, resource limits, cancellation, and history."
    Config = JobsConfig
    State = JobsState
    events = (JobLog, JobFinished)

    def __init__(self, runtime: Runtime) -> None:
        super().__init__(runtime)
        self._jobs: dict[str, _Job] = {}
        self._history: list[JobSummary] = []
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._finish_hooks: list[Callable[[JobSummary], None]] = []
        self._flush_scheduled = False

    async def start(self) -> None:
        await self.runtime.db.execute(
            "UPDATE jobs SET status = ?, error = ?, ended_at = ? WHERE status IN (?, ?)",
            (JobStatus.FAILED, "interrupted by a dashboard restart", time.time(), JobStatus.QUEUED, JobStatus.RUNNING),
        )
        rows = await self.runtime.db.fetch_all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (RECENT_JOBS,))
        self._history = [_summary_from_row(row) for row in rows]
        self._refresh_state()

    async def stop(self) -> None:
        # Await the tasks, not `finished`: a task cancelled before its first step never runs its body.
        tasks = [job.task for job in self._jobs.values() if job.task is not None and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @override
    async def on_config_changed(self, previous: JobsConfig) -> None:
        for name in ("agent", "checks"):
            self._semaphores.pop(name, None)

    def on_finished(self, hook: Callable[[JobSummary], None]) -> None:
        """Call `hook` with the final summary of every job that ends."""
        self._finish_hooks.append(hook)

    # --- submitting ---

    async def submit(
        self,
        *,
        kind: str,
        title: str,
        work: WorkFn,
        project_id: str | None = None,
        resources: Iterable[str] = (),
    ) -> JobRef:
        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        log_path = self.runtime.paths.jobs / f"{job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        summary = JobSummary(
            id=job_id, kind=kind, title=title, project_id=project_id, status=JobStatus.QUEUED, created_at=time.time()
        )
        job = _Job(summary=summary, resources=tuple(sorted(set(resources))), log_path=log_path, work=work)
        self._jobs[job_id] = job
        await self.runtime.db.execute(
            "INSERT INTO jobs (id, kind, title, project_id, status, created_at, log_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, kind, title, project_id, summary.status, summary.created_at, str(log_path)),
        )
        job.task = asyncio.create_task(self._execute(job), name=f"job:{job_id}")
        self._refresh_state()
        return JobRef(job_id=job_id, kind=kind, title=title)

    async def wait(self, job_id: str) -> JobSummary:
        """Wait for a job submitted in this process to finish and return its final summary."""
        job = self._jobs.get(job_id)
        if job is not None:
            await job.finished.wait()
        return await self._load_summary(job_id)

    def _semaphore(self, resource: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(resource)
        if semaphore is None:
            limit = {"agent": self.config.agent_concurrency, "checks": self.config.check_concurrency}.get(resource, 1)
            semaphore = self._semaphores[resource] = asyncio.Semaphore(limit)
        return semaphore

    async def _execute(self, job: _Job) -> None:
        try:
            outcome, error = await self._perform(job)
            await self._finish(job, outcome, error)
        finally:
            self._jobs.pop(job.summary.id, None)
            job.finished.set()

    async def _perform(self, job: _Job) -> tuple[JobOutcome, str | None]:
        """Acquire the job's resources, run its work, and turn every ending into an outcome."""
        async with contextlib.AsyncExitStack() as stack:
            try:
                for resource in job.resources:
                    semaphore = self._semaphore(resource)
                    if semaphore.locked():
                        self._update(job, waiting_for=resource)
                    await stack.enter_async_context(semaphore)
                job.log_file = stack.enter_context(job.log_path.open("a", encoding="utf-8"))
                await self._transition(job, JobStatus.RUNNING, started_at=time.time(), waiting_for=None)
                return await job.work(JobContext(self, job)), None
            except asyncio.CancelledError:
                return JobOutcome(status=JobStatus.CANCELLED, summary="cancelled"), None
            except ActionError as failure:
                self._append(job, f"error: {failure.message}")
                return JobOutcome(status=JobStatus.FAILED), failure.message
            except Exception as failure:  # a job's own bug must end the job, not the dashboard
                error = f"{type(failure).__name__}: {failure}"
                self._append(job, f"error: {error}")
                self.runtime.log.exception("job %s crashed", job.summary.id)
                return JobOutcome(status=JobStatus.FAILED), error
            finally:
                self._flush_log(job)
                job.log_file = None

    async def _finish(self, job: _Job, outcome: JobOutcome, error: str | None) -> None:
        result = outcome.result.model_dump(mode="json") if outcome.result is not None else None
        await self._transition(
            job,
            outcome.status,
            ended_at=time.time(),
            summary=outcome.summary or job.summary.summary,
            error=error,
            progress=None if outcome.status is not JobStatus.SUCCEEDED else 1.0,
            result=result,
        )
        final = job.summary
        self.publish(
            JobFinished(
                project_id=final.project_id,
                job_id=final.id,
                kind=final.kind,
                job_title=final.title,
                status=final.status,
                summary=final.summary or final.error,
            )
        )
        for hook in self._finish_hooks:
            hook(final)

    async def _transition(
        self, job: _Job, status: JobStatus, *, result: dict[str, JsonValue] | None = None, **changes: object
    ) -> None:
        self._update(job, status=status, **changes)
        summary = job.summary
        await self.runtime.db.execute(
            "UPDATE jobs SET status = ?, started_at = ?, ended_at = ?, summary = ?, error = ?, "
            "result = COALESCE(?, result) WHERE id = ?",
            (
                summary.status,
                summary.started_at,
                summary.ended_at,
                summary.summary,
                summary.error,
                json.dumps(result) if result is not None else None,
                summary.id,
            ),
        )
        if status in FINISHED:
            self._history = [summary, *[item for item in self._history if item.id != summary.id]][:RECENT_JOBS]
            self._refresh_state()

    def _update(self, job: _Job, **changes: object) -> None:
        job.summary = job.summary.model_copy(update=changes)
        self._refresh_state()

    def _refresh_state(self) -> None:
        live = [job.summary for job in self._jobs.values()]
        live_ids = {summary.id for summary in live}
        ordered = sorted(live, key=lambda s: (s.status is not JobStatus.RUNNING, -s.created_at))
        jobs = [*ordered, *(item for item in self._history if item.id not in live_ids)][:RECENT_JOBS]
        self.set_state(
            JobsState(
                jobs=jobs,
                running=sum(1 for s in live if s.status is JobStatus.RUNNING),
                queued=sum(1 for s in live if s.status is JobStatus.QUEUED),
            )
        )

    # --- logs ---

    def _append(self, job: _Job, line: str) -> None:
        if job.log_file is not None:
            job.log_file.write(line + "\n")
        if not job.pending:
            job.pending_offset = job.line_count
        job.pending.append(line)
        job.line_count += 1
        if not self._flush_scheduled:
            self._flush_scheduled = True
            asyncio.get_running_loop().call_later(LOG_BATCH_S, self._flush_all)

    def _flush_all(self) -> None:
        self._flush_scheduled = False
        for job in list(self._jobs.values()):
            self._flush_log(job)

    def _flush_log(self, job: _Job) -> None:
        if job.log_file is not None:
            job.log_file.flush()
        if not job.pending:
            return
        lines, job.pending = job.pending, []
        self.publish(
            JobLog(project_id=job.summary.project_id, job_id=job.summary.id, offset=job.pending_offset, lines=lines)
        )

    async def _load_summary(self, job_id: str) -> JobSummary:
        live = self._jobs.get(job_id)
        if live is not None:
            return live.summary
        row = await self.runtime.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise ActionError(f"no job {job_id!r}", status=404)
        return _summary_from_row(row)

    # --- actions ---

    @action(title="Job details", query=True)
    async def get(self, request: JobIdInput) -> JobDetail:
        """A job's summary and result."""
        summary = await self._load_summary(request.job_id)
        row = await self.runtime.db.fetch_one("SELECT result FROM jobs WHERE id = ?", (request.job_id,))
        result = json.loads(row["result"]) if row is not None and row["result"] else None
        return JobDetail(job=summary, result=result)

    @action(title="Job log", query=True)
    async def log(self, request: JobLogInput) -> JobLogChunk:
        """Lines of a job's log from `offset`."""
        summary = await self._load_summary(request.job_id)
        row = await self.runtime.db.fetch_one("SELECT log_path FROM jobs WHERE id = ?", (request.job_id,))
        if row is None:
            raise ActionError(f"no job {request.job_id!r}", status=404)
        path = Path(row["log_path"])
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
        chunk = lines[request.offset : request.offset + request.limit]
        return JobLogChunk(
            job_id=request.job_id,
            offset=request.offset,
            next_offset=request.offset + len(chunk),
            lines=chunk,
            done=summary.status in FINISHED,
        )

    @action(title="Cancel job", confirm=True)
    async def cancel(self, request: JobIdInput) -> Done:
        """Stop a queued or running job; its processes are killed."""
        job = self._jobs.get(request.job_id)
        if job is None or job.task is None:
            raise ActionError("the job is not running", status=409)
        job.task.cancel()
        await asyncio.wait([job.task])
        return Done(message="cancelled")

    @action(title="Job history", query=True)
    async def history(self, request: JobListInput) -> JobList:
        """Job history, optionally for one project or kind."""
        clauses: list[str] = []
        params: list[object] = []
        if request.project_id is not None:
            clauses.append("project_id = ?")
            params.append(request.project_id)
        if request.kind is not None:
            clauses.append("kind = ?")
            params.append(request.kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.runtime.db.fetch_all(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?", (*params, request.limit)
        )
        return JobList(
            jobs=[self._jobs[row["id"]].summary if row["id"] in self._jobs else _summary_from_row(row) for row in rows]
        )


def _summary_from_row(row: sqlite3.Row) -> JobSummary:
    return JobSummary(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        project_id=row["project_id"],
        status=JobStatus(row["status"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        summary=row["summary"],
        error=row["error"],
    )
