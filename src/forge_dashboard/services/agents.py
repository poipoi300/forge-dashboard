"""Agent runs through the configurable harness, their transcripts and history, and Claude Code insight.

Every agent run:
- streams a live transcript from stream-json into the job log and a file;
- records cost, turns, and duration;
- is cancellable (the job kills its process tree) and logged;
- holds the `agent` resource, so one runs at a time by default;
- runs only inside a dashboard worktree, never on a user's working tree.

The service also owns branch runs (work landed on a worktree branch for
review), the delegate-issue and update-dependencies flows, and read-only
insight into Claude Code sessions and running `claude` processes per project.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, override

from pydantic import BaseModel, Field, JsonValue

from forge_dashboard import gitops, worktrees
from forge_dashboard.branchwork import BranchRun, BranchRunState, BranchWork, CheckOutcomeBrief
from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, JobRef, NoInput, ProjectRef, Service, action
from forge_dashboard.core.tasks import repeat
from forge_dashboard.core.ui import ui
from forge_dashboard.domain import claude_sessions, harness
from forge_dashboard.domain.claude_sessions import ClaudeProcess, ClaudeSession
from forge_dashboard.domain.harness import HarnessConfig, RunKind, RunKinds, RunResult
from forge_dashboard.process import tree
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

INSIGHT_REFRESH_S = 30
TRANSCRIPT_BATCH_S = 0.2

AgentOutcomeName = Literal["succeeded", "failed", "cancelled", "timeout", "harness-missing"]


class IssueBrief(BaseModel):
    number: int
    title: str
    body: str
    url: str | None = None


class IssueSource(Protocol):
    """What delegating an issue needs from GitHub."""

    async def issue_brief(self, project_id: str, number: int) -> IssueBrief: ...


class PullRequestOpener(Protocol):
    """What opening a PR for a branch run needs from GitHub."""

    async def open_pull_request(self, project_id: str, head: str, base: str, title: str, body: str) -> str: ...


class AgentRunRecord(BaseModel):
    id: str = Field(json_schema_extra=ui(format="code", hidden=True))
    job_id: str = Field(json_schema_extra=ui(hidden=True))
    kind: str = Field(json_schema_extra=ui(format="badge"))
    project_id: str | None = Field(None, json_schema_extra=ui(short_label="project"))
    started_at: float = Field(json_schema_extra=ui(format="relative-time", short_label="started"))
    ended_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", hidden=True))
    model: str | None = None
    outcome: AgentOutcomeName | Literal["running"] = Field(
        "running",
        json_schema_extra=ui(
            format="badge",
            tones={
                "running": "info",
                "succeeded": "good",
                "failed": "bad",
                "cancelled": "neutral",
                "timeout": "warn",
                "harness-missing": "bad",
            },
        ),
    )
    exit_code: int | None = None
    cost_usd: float | None = Field(None, json_schema_extra=ui(unit="USD", short_label="cost"))
    turns: int | None = None
    duration_ms: int | None = Field(None, json_schema_extra=ui(short_label="duration", unit="ms"))
    transcript_path: str = Field(json_schema_extra=ui(format="path", hidden=True))


class AgentsConfig(BaseModel):
    harness: HarnessConfig = Field(default_factory=HarnessConfig, json_schema_extra=ui(group="Harness"))
    kinds: RunKinds = Field(default_factory=RunKinds, json_schema_extra=ui(group="Run kinds"))
    claude_projects_dir: str | None = Field(
        None,
        json_schema_extra=ui(
            widget="path-dir", advanced=True, help="Claude Code transcripts; defaults to ~/.claude/projects."
        ),
    )


class ProjectInsight(BaseModel):
    project_id: str
    active_processes: list[ClaudeProcess] = Field(
        default_factory=list, json_schema_extra=ui(columns=["pid", "started_at"])
    )
    session_count: int = 0
    last_session_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))


class AgentsState(BaseModel):
    harness_available: bool = Field(False, json_schema_extra=ui(help="Whether the harness executable is on PATH."))
    harness_executable: str = Field("claude", json_schema_extra=ui(format="code"))
    runs: list[AgentRunRecord] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["kind", "project_id", "outcome", "cost_usd", "turns", "started_at"]),
    )
    branch_runs: list[BranchRun] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["title", "project_id", "state", "files_changed", "updated_at"]),
    )
    insight: list[ProjectInsight] = Field(default_factory=list)


class AgentRun(BaseModel):
    """The outcome of one harness invocation, as returned to the flow that started it."""

    run_id: str
    outcome: AgentOutcomeName
    result: RunResult | None = None
    exit_code: int | None = None


class TranscriptInput(BaseModel):
    run_id: str
    offset: int = Field(0, ge=0)
    limit: int = Field(500, ge=1, le=5000)


class Transcript(BaseModel):
    run_id: str
    records: list[dict[str, JsonValue]]
    next_offset: int
    done: bool
    prompt: str | None = Field(None, json_schema_extra=ui(format="code"))


class RunsInput(BaseModel):
    project_id: str | None = Field(None, json_schema_extra=ui(bind="project"))
    limit: int = Field(50, ge=1, le=500)


class Runs(BaseModel):
    runs: list[AgentRunRecord] = Field(
        json_schema_extra=ui(columns=["kind", "outcome", "cost_usd", "turns", "started_at"])
    )


class Sessions(BaseModel):
    sessions: list[ClaudeSession] = Field(
        json_schema_extra=ui(columns=["start", "duration_s", "first_prompt", "message_count"])
    )
    processes: list[ClaudeProcess] = Field(json_schema_extra=ui(columns=["pid", "started_at", "cwd"]))


class BranchRunInput(BaseModel):
    run_id: int = Field(json_schema_extra=ui(hidden=True))


class BranchRunsInput(BaseModel):
    project_id: str | None = Field(None, json_schema_extra=ui(bind="project"))
    kind: str | None = None


class BranchRuns(BaseModel):
    runs: list[BranchRun] = Field(json_schema_extra=ui(columns=["title", "state", "files_changed", "updated_at"]))


class BranchDiff(BaseModel):
    run: BranchRun
    diff: str = Field(json_schema_extra=ui(format="code"))


class DelegateInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    issue_number: int = Field(ge=1)
    instructions: str | None = Field(None, json_schema_extra=ui(widget="textarea", placeholder="Extra guidance"))


class UpdateDepsInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    instructions: str | None = Field(None, json_schema_extra=ui(widget="textarea", placeholder="Scope or exclusions"))


class OpenPrInput(BaseModel):
    run_id: int = Field(json_schema_extra=ui(hidden=True))
    title: str | None = Field(None, json_schema_extra=ui(help="Defaults to the run's title."))
    body: str | None = Field(None, json_schema_extra=ui(widget="textarea"))


class HarnessStatus(BaseModel):
    available: bool
    executable: str
    resolved: str | None


class AgentRunFinished(Event):
    type: ClassVar[str] = "agents.run-finished"
    persist: ClassVar[bool] = True
    run_id: str
    kind: str
    outcome: str
    cost_usd: float | None
    turns: int | None

    def title(self) -> str:
        cost = f", ${self.cost_usd:.3f}" if self.cost_usd is not None else ""
        return f"Agent {self.kind.replace('_', ' ')}: {self.outcome}{cost}"

    def severity(self) -> Severity:
        return "success" if self.outcome == "succeeded" else "error"


class AgentTranscript(Event):
    type: ClassVar[str] = "agents.transcript"
    run_id: str
    offset: int
    records: list[dict[str, JsonValue]]


class BranchRunUpdated(Event):
    type: ClassVar[str] = "agents.branch-run"
    persist: ClassVar[bool] = True
    run_id: int
    kind: str
    state: str
    run_title: str

    def title(self) -> str:
        return f"{self.run_title}: {self.state}"

    def dedupe_key(self) -> str:
        return f"branch-run:{self.run_id}:{self.state}"


UPDATE_DEPS_GUIDANCE = (
    "Use each ecosystem's own tool to update lockfiles (uv lock --upgrade, bun update, cargo update) "
    "and fix any breakage so the existing checks pass."
)


async def base_branch_of(record: ProjectRecord) -> str:
    """The branch work starts from: the manifest's `default_branch` when it sets one, else the repo's own."""
    return await gitops.default_branch(record.path, declared=record.declared_default_branch)


def _git_rules() -> str:
    return (
        "Rules: edit files only. Do not run git; the dashboard runs every git command. Keep every existing "
        "gate and test; never weaken a gate, lower a threshold, or add blanket ignores."
    )


class AgentsService(Service[AgentsConfig, AgentsState], ProjectListener):
    id = "agents"
    title = "Agents"
    description = (
        "Agent runs through the harness with live transcripts, cost, and history; branch work; Claude Code insight."
    )
    Config = AgentsConfig
    State = AgentsState
    events = (AgentRunFinished, AgentTranscript, BranchRunUpdated)

    def __init__(self, runtime: Runtime, projects: ProjectsService, jobs: JobsService, checks: ChecksService) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._jobs = jobs
        self._checks = checks
        self._issues: IssueSource | None = None
        self._pulls: PullRequestOpener | None = None
        self._insight_task: asyncio.Task[None] | None = None
        self._index = claude_sessions.TranscriptIndex()
        self._branch_work: BranchWork | None = None
        projects.listen(self)

    def connect_github(self, issues: IssueSource, pulls: PullRequestOpener) -> None:
        self._issues = issues
        self._pulls = pulls

    @property
    def branch_work(self) -> BranchWork:
        if self._branch_work is None:
            self._branch_work = BranchWork(self.runtime.db, self.runtime.paths.worktrees)
        return self._branch_work

    # --- lifecycle ---

    @override
    async def start(self) -> None:
        await self.runtime.db.execute(
            "UPDATE agent_runs SET outcome = 'failed', ended_at = ? WHERE outcome = 'running'", (time.time(),)
        )
        await self.branch_work.fail_interrupted()
        await self._refresh_lists()
        self._refresh_harness()
        self._insight_task = asyncio.create_task(
            repeat(self._insight_once, lambda: INSIGHT_REFRESH_S, label="agent insight"), name="agents-insight"
        )

    @override
    async def stop(self) -> None:
        if self._insight_task is not None:
            self._insight_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._insight_task

    @override
    async def on_config_changed(self, previous: AgentsConfig) -> None:
        self._refresh_harness()

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        await self._insight_for([record], await asyncio.to_thread(claude_sessions.running_claude_processes))

    def _refresh_harness(self) -> None:
        executable = self.config.harness.command[0] if self.config.harness.command else ""
        self.update_state(harness_executable=executable, harness_available=shutil.which(executable) is not None)

    async def _refresh_lists(self) -> None:
        rows = await self.runtime.db.fetch_all("SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT 50")
        runs = [AgentRunRecord.model_validate(dict(row)) for row in rows]
        self.update_state(runs=runs, branch_runs=await self.branch_work.recent(limit=50))

    # --- Claude Code insight ---

    def _projects_dir(self) -> Path:
        configured = self.config.claude_projects_dir
        return Path(configured) if configured else claude_sessions.default_projects_dir()

    async def _insight_once(self) -> None:
        processes = await asyncio.to_thread(claude_sessions.running_claude_processes)
        await self._insight_for(self._projects.records(), processes)

    async def _insight_for(self, records: list[ProjectRecord], processes: list[ClaudeProcess]) -> None:
        projects_dir = self._projects_dir()
        current = {item.project_id: item for item in self.state.insight}
        for record in records:
            sessions = await asyncio.to_thread(
                claude_sessions.sessions_for_project, projects_dir, record.path, index=self._index
            )
            inside = claude_sessions.processes_inside(processes, record.path)
            facts = claude_sessions.agent_facts(sessions, inside)
            self._projects.put(record.id, facts)
            current[record.id] = ProjectInsight(
                project_id=record.id,
                active_processes=inside,
                session_count=facts.session_count,
                last_session_at=facts.last_session_at,
            )
        known = {record.id for record in self._projects.records()}
        self.update_state(insight=sorted((v for k, v in current.items() if k in known), key=lambda i: i.project_id))

    # --- running the harness ---

    def _check_isolated(self, cwd: Path) -> None:
        root = self.runtime.paths.worktrees.resolve()
        if root != cwd.resolve() and root not in cwd.resolve().parents:
            raise ActionError(f"agent runs happen only in dashboard worktrees; refused {cwd}", status=409)

    async def run_agent(
        self, context: JobContext, *, kind: RunKind, project_id: str | None, prompt: str, cwd: Path
    ) -> AgentRun:
        """Run the harness in a dashboard worktree, holding the agent slot, and record everything."""
        self._check_isolated(cwd)
        settings = getattr(self.config.kinds, kind)
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        transcript_path = self.runtime.paths.transcripts / f"{run_id}.jsonl"
        prompt_path = self.runtime.paths.transcripts / f"{run_id}.prompt.md"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        argv = harness.render_argv(self.config.harness, settings, prompt, str(prompt_path))
        stdin = harness.render_stdin(self.config.harness, prompt, str(prompt_path))
        record = AgentRunRecord(
            id=run_id,
            job_id=context.job_id,
            kind=kind,
            project_id=project_id,
            started_at=time.time(),
            model=settings.model,
            transcript_path=str(transcript_path),
        )
        async with context.hold("agent"):
            await self._insert_run(record)
            context.log(f"agent: {kind.replace('_', ' ')} run {run_id} in {cwd}")
            outcome = await self._stream(context, record, argv, stdin, cwd, transcript_path, settings.timeout_s)
        await self._close_run(outcome, record)
        return outcome

    async def _stream(
        self,
        context: JobContext,
        record: AgentRunRecord,
        argv: list[str],
        stdin: bytes | None,
        cwd: Path,
        transcript_path: Path,
        timeout_s: int,
    ) -> AgentRun:
        result: RunResult | None = None
        pending: list[dict[str, JsonValue]] = []
        offset = 0
        loop = asyncio.get_running_loop()
        flush_scheduled = False

        def flush() -> None:
            nonlocal pending, offset, flush_scheduled
            flush_scheduled = False
            if pending:
                self.publish(
                    AgentTranscript(project_id=record.project_id, run_id=record.id, offset=offset, records=pending)
                )
                offset += len(pending)
                pending = []

        with transcript_path.open("w", encoding="utf-8") as transcript:

            def on_line(line: str) -> None:
                nonlocal result, flush_scheduled
                parsed = harness.parse_record(line)
                if parsed is None:
                    if line.strip():
                        context.log(f"harness: {line}")
                    return
                transcript.write(json.dumps(parsed) + "\n")
                transcript.flush()
                for described in harness.describe_record(parsed):
                    context.log(described)
                if parsed.get("type") == "result":
                    result = harness.result_of(parsed)
                pending.append(parsed)
                if not flush_scheduled:
                    flush_scheduled = True
                    loop.call_later(TRANSCRIPT_BATCH_S, flush)

            try:
                completed = await tree.stream(argv, cwd, tree.child_env(), on_line, timeout_s=timeout_s, stdin=stdin)
            except tree.ExecutableNotFoundError as error:
                context.log(f"agent: {error}")
                return AgentRun(run_id=record.id, outcome="harness-missing")
            except asyncio.CancelledError:
                flush()
                await self._close_run(AgentRun(run_id=record.id, outcome="cancelled"), record)
                raise
        flush()
        if completed.timed_out:
            return AgentRun(run_id=record.id, outcome="timeout", result=result)
        failed = completed.exit_code != 0 or (result is not None and result.is_error)
        return AgentRun(
            run_id=record.id, outcome="failed" if failed else "succeeded", result=result, exit_code=completed.exit_code
        )

    async def _insert_run(self, record: AgentRunRecord) -> None:
        await self.runtime.db.execute(
            "INSERT INTO agent_runs (id, job_id, kind, project_id, started_at, model, outcome, transcript_path) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                record.id,
                record.job_id,
                record.kind,
                record.project_id,
                record.started_at,
                record.model,
                "running",
                record.transcript_path,
            ),
        )
        await self._refresh_lists()

    async def _close_run(self, outcome: AgentRun, record: AgentRunRecord) -> None:
        result = outcome.result
        await self.runtime.db.execute(
            "UPDATE agent_runs SET ended_at = ?, outcome = ?, exit_code = ?, cost_usd = ?, turns = ?, duration_ms = ? "
            "WHERE id = ?",
            (
                time.time(),
                outcome.outcome,
                outcome.exit_code,
                result.cost_usd if result else None,
                result.turns if result else None,
                result.duration_ms if result else None,
                record.id,
            ),
        )
        self.publish(
            AgentRunFinished(
                project_id=record.project_id,
                run_id=record.id,
                kind=record.kind,
                outcome=outcome.outcome,
                cost_usd=result.cost_usd if result else None,
                turns=result.turns if result else None,
            )
        )
        await self._refresh_lists()

    # --- branch-run flows ---

    def guard_branch_run(self, run: BranchRun, repo: Path) -> AbstractAsyncContextManager[None]:
        """Wrap the work on a branch run so that an error or a cancellation never leaves it `running`."""
        return self.branch_work.guarded(run, repo, on_change=self._branch_changed)

    async def branch_flow(
        self,
        *,
        record: ProjectRecord,
        kind: Literal["delegate", "update-deps"],
        title: str,
        branch_prefix: str,
        prompt_for: Callable[[Path], Awaitable[str]],
        run_kind: RunKind,
    ) -> JobRef:
        """Worktree branch, agent edits, project checks, then a diff waiting for review."""
        if not record.is_git:
            raise ActionError(f"{record.path} is not a git repository", status=409)

        async def work(context: JobContext) -> JobOutcome:
            run, worktree = await self.branch_work.start(
                repo=record.path,
                project_id=record.id,
                kind=kind,
                title=title,
                branch_prefix=branch_prefix,
                base_branch=await base_branch_of(record),
                job_id=context.job_id,
            )
            async with self.guard_branch_run(run, record.path):
                await self._branch_changed(run)
                agent = await self.run_agent(
                    context,
                    kind=run_kind,
                    project_id=record.id,
                    prompt=await prompt_for(worktree.path),
                    cwd=worktree.path,
                )
                run = run.model_copy(update={"agent_run_ids": [agent.run_id], "agent_attempts": 1})
                if agent.outcome != "succeeded":
                    run = await self.branch_work.finish(
                        run, record.path, summary="agent run failed", error=agent.outcome
                    )
                    await self._branch_changed(run)
                    return JobOutcome(status=JobStatus.FAILED, summary=f"agent {agent.outcome}")
                await worktrees.commit_all(
                    worktree.path, f"{title}\n\nWritten by an agent run ({agent.run_id}) from the forge dashboard."
                )
                checks = record.manifest.checks if record.manifest is not None else []
                results = await self._checks.run_checks_in(
                    context,
                    record,
                    list(checks),
                    worktree.path,
                    record_results=False,
                    on_head=True,
                    sha=None,
                    dirty=False,
                )
                run = run.model_copy(
                    update={"checks": [CheckOutcomeBrief(check_id=c.id, label=c.label, outcome=o) for c, o in results]}
                )
                failing = [c.label for c, o in results if o != "passed"]
                summary = "checks passing" if not failing else f"checks failing: {', '.join(failing)}"
                run = await self.branch_work.finish(run, record.path, summary=summary)
                await self._branch_changed(run)
                status = JobStatus.ATTENTION if run.state is BranchRunState.READY else JobStatus.FAILED
                return JobOutcome(status=status, summary=f"{run.files_changed} files changed; {summary}")

        return await self._jobs.submit(
            kind=f"agents.{kind}", title=title, work=work, project_id=record.id, resources=[f"repo:{record.id}"]
        )

    async def _branch_changed(self, run: BranchRun) -> None:
        self.publish(
            BranchRunUpdated(
                project_id=run.project_id, run_id=run.id, kind=run.kind, state=run.state, run_title=run.title
            )
        )
        await self._refresh_lists()

    # --- actions ---

    @action(title="Delegate issue to agent", long_running=True, confirm=True)
    async def delegate_issue(self, request: DelegateInput) -> JobRef:
        """The agent implements a GitHub issue on a worktree branch; checks run; the diff waits for review."""
        record = self._projects.record(request.project_id)
        if self._issues is None:
            raise ActionError("GitHub is not connected", status=409)
        issue = await self._issues.issue_brief(record.id, request.issue_number)

        async def prompt(worktree: Path) -> str:
            agents_md = (
                (worktree / "AGENTS.md").read_text(encoding="utf-8") if (worktree / "AGENTS.md").is_file() else ""
            )
            return "\n\n".join(
                part
                for part in (
                    f"Implement GitHub issue #{issue.number} in this repository: {issue.title}",
                    f"Issue description:\n{issue.body or '(no description)'}",
                    f"Additional instructions from the maintainer:\n{request.instructions}"
                    if request.instructions
                    else "",
                    "Implement it completely with tests, following the repository's AGENTS.md. " + _git_rules(),
                    f"The repository's AGENTS.md:\n{agents_md}" if agents_md else "",
                )
                if part
            )

        return await self.branch_flow(
            record=record,
            kind="delegate",
            title=f"Issue #{issue.number}: {issue.title}",
            branch_prefix=f"forge-issue/{issue.number}",
            prompt_for=prompt,
            run_kind="delegate",
        )

    @action(title="Update dependencies", long_running=True, confirm=True)
    async def update_dependencies(self, request: UpdateDepsInput) -> JobRef:
        """The agent updates dependencies on a branch; checks run; the diff waits for review."""
        record = self._projects.record(request.project_id)
        languages = ", ".join(record.languages) or "unknown"

        async def prompt(_worktree: Path) -> str:
            return "\n\n".join(
                part
                for part in (
                    f"Update this repository's dependencies to their latest compatible versions ({languages}).",
                    UPDATE_DEPS_GUIDANCE,
                    f"Scope from the maintainer:\n{request.instructions}" if request.instructions else "",
                    _git_rules(),
                )
                if part
            )

        return await self.branch_flow(
            record=record,
            kind="update-deps",
            title="Update dependencies",
            branch_prefix="forge-deps",
            prompt_for=prompt,
            run_kind="update_deps",
        )

    @action(title="Agent runs", query=True)
    async def runs(self, request: RunsInput) -> Runs:
        """Agent run history with cost, turns, and duration."""
        where, params = ("WHERE project_id = ?", (request.project_id,)) if request.project_id else ("", ())
        rows = await self.runtime.db.fetch_all(
            f"SELECT * FROM agent_runs {where} ORDER BY started_at DESC LIMIT ?", (*params, request.limit)
        )
        return Runs(runs=[AgentRunRecord.model_validate(dict(row)) for row in rows])

    @action(title="Transcript", query=True)
    async def transcript(self, request: TranscriptInput) -> Transcript:
        """A run's stream-json records from `offset`, and its prompt."""
        row = await self.runtime.db.fetch_one("SELECT * FROM agent_runs WHERE id = ?", (request.run_id,))
        if row is None:
            raise ActionError(f"no agent run {request.run_id!r}", status=404)
        path = Path(row["transcript_path"])
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        chunk = [json.loads(line) for line in lines[request.offset : request.offset + request.limit] if line]
        prompt_file = path.with_name(path.name.removesuffix(".jsonl") + ".prompt.md")
        return Transcript(
            run_id=request.run_id,
            records=chunk,
            next_offset=request.offset + len(chunk),
            done=row["outcome"] != "running",
            prompt=prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else None,
        )

    @action(title="Claude Code sessions", query=True)
    async def sessions(self, request: ProjectRef) -> Sessions:
        """Recent Claude Code sessions in the project and running claude processes inside it."""
        record = self._projects.record(request.project_id)
        found = await asyncio.to_thread(
            claude_sessions.sessions_for_project, self._projects_dir(), record.path, index=self._index
        )
        processes = await asyncio.to_thread(claude_sessions.active_claude_processes, record.path)
        return Sessions(sessions=found[:100], processes=processes)

    @action(title="Harness status", query=True)
    async def harness_status(self, _request: NoInput) -> HarnessStatus:
        """Whether the configured harness can be started."""
        self._refresh_harness()
        executable = self.state.harness_executable
        return HarnessStatus(
            available=self.state.harness_available, executable=executable, resolved=shutil.which(executable)
        )

    @action(title="Branch runs", query=True)
    async def branch_runs(self, request: BranchRunsInput) -> BranchRuns:
        """Work waiting on worktree branches, newest first."""
        return BranchRuns(runs=await self.branch_work.recent(project_id=request.project_id, kind=request.kind))

    @action(title="Branch diff", query=True)
    async def branch_diff(self, request: BranchRunInput) -> BranchDiff:
        """The run's changes against the base it started from."""
        run = await self.branch_work.get(request.run_id)
        record = self._projects.record(run.project_id)
        return BranchDiff(run=run, diff=await self.branch_work.diff(run, record.path))

    @action(title="Merge", confirm=True)
    async def merge_branch(self, request: BranchRunInput) -> BranchRun:
        """Fast-forward the base branch to this work, when it is clean; otherwise keep the branch."""
        run = await self.branch_work.get(request.run_id)
        merged = await self.branch_work.merge(run, self._projects.record(run.project_id).path)
        await self._branch_changed(merged)
        return merged

    @action(title="Discard", danger=True, confirm=True)
    async def discard_branch(self, request: BranchRunInput) -> BranchRun:
        """Remove the worktree and delete the branch."""
        run = await self.branch_work.get(request.run_id)
        discarded = await self.branch_work.discard(run, self._projects.record(run.project_id).path)
        await self._branch_changed(discarded)
        return discarded

    @action(title="Open PR", confirm=True)
    async def open_pr(self, request: OpenPrInput) -> BranchRun:
        """Push the branch to origin and open a pull request (both are writes to GitHub)."""
        run = await self.branch_work.get(request.run_id)
        if run.state is not BranchRunState.READY:
            raise ActionError(f"run is {run.state.value}, not ready", status=409)
        if self._pulls is None:
            raise ActionError("GitHub is not connected", status=409)
        record = self._projects.record(run.project_id)
        await gitops.run(record.path, "push", "-u", "--", "origin", run.branch, timeout_s=300)
        body = request.body or f"{run.summary or ''}\n\n```\n{run.diffstat}\n```\n\nOpened from the forge dashboard."
        url = await self._pulls.open_pull_request(
            record.id, run.branch, run.base_branch, request.title or run.title, body
        )
        opened = await self.branch_work.save(run.model_copy(update={"state": BranchRunState.PR_OPENED, "pr_url": url}))
        await self._branch_changed(opened)
        return opened
