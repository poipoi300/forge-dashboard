"""Local checks from manifests, the project's pre-commit gates, and reported coverage.

Checks run in the working tree by default (it reflects the current state), or
on HEAD in a temporary worktree. JUnit reports are parsed; every run is stored
with its commit and a dirty flag. Schedules are per project: on change
(debounced), on an interval, or manual. The jobs service enforces the global
limit (`checks`) and one run per repo (`repo:<id>`).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, override

import yaml
from pydantic import BaseModel, Field

from forge_dashboard import worktrees
from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, Done, JobRef, ProjectRef, Service, action
from forge_dashboard.core.tasks import BackgroundTasks
from forge_dashboard.core.ui import ui
from forge_dashboard.domain.commands import render_command
from forge_dashboard.domain.coverage import CoverageReport, combined_coverage
from forge_dashboard.domain.junit import JUnitError, JUnitReport, read_junit
from forge_dashboard.domain.scheduling import IntervalSchedule, ManualSchedule
from forge_dashboard.domain.summary import CheckFacts
from forge_dashboard.manifest.models import Check
from forge_dashboard.services.discovery import ChangeKind
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService
from forge_dashboard.services.scheduler import SchedulerService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

Outcome = Literal["passed", "failed", "error", "timeout"]
OUTCOME_UI = ui(
    format="badge",
    tones={"passed": "good", "failed": "bad", "error": "bad", "timeout": "warn", "never": "neutral", "running": "info"},
)


class OnChangeSchedule(BaseModel):
    """Run after the working tree settles."""

    kind: Literal["on-change"] = "on-change"
    debounce_s: int = Field(20, ge=2, le=3600, json_schema_extra=ui(widget="duration"))


CheckSchedule = Annotated[ManualSchedule | IntervalSchedule | OnChangeSchedule, Field(discriminator="kind")]


class CheckSettings(BaseModel):
    """How and when a project's checks run."""

    schedule: CheckSchedule = Field(default_factory=ManualSchedule)
    on_head: bool = Field(
        False, json_schema_extra=ui(help="Run on HEAD in a temporary worktree, ignoring local edits.")
    )


class CheckRunSummary(BaseModel):
    check_id: str
    label: str
    stage: str = Field(json_schema_extra=ui(format="badge"))
    outcome: Outcome | Literal["never", "running"] = Field("never", json_schema_extra=OUTCOME_UI)
    exit_code: int | None = None
    tests: int | None = None
    failures: int | None = None
    errors: int | None = None
    skipped: int | None = None
    duration_s: float | None = Field(None, json_schema_extra=ui(format="duration"))
    sha: str | None = Field(None, json_schema_extra=ui(format="code"))
    dirty: bool = False
    on_head: bool = False
    ran_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    stale: bool = False
    job_id: str | None = Field(None, json_schema_extra=ui(hidden=True))


class Gate(BaseModel):
    id: str = Field(json_schema_extra=ui(format="code"))
    name: str
    stages: list[str]


class ProjectChecks(BaseModel):
    project_id: str
    checks: list[CheckRunSummary] = Field(
        default_factory=list, json_schema_extra=ui(columns=["label", "outcome", "tests", "duration_s", "ran_at"])
    )
    gates: list[Gate] = Field(default_factory=list, json_schema_extra=ui(columns=["id", "name", "stages"]))
    coverage: CoverageReport | None = None
    settings: CheckSettings = Field(default_factory=CheckSettings)


class ChecksConfig(BaseModel):
    keep_runs: int = Field(500, ge=10, le=10_000, json_schema_extra=ui(advanced=True, help="Runs kept per check."))


class ChecksState(BaseModel):
    projects: list[ProjectChecks] = Field(default_factory=list)


class RunChecksInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    check_id: str | None = Field(None, json_schema_extra=ui(help="One check; empty runs all of them."))
    on_head: bool | None = Field(None, json_schema_extra=ui(help="Override the project's setting for this run."))


class GateInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    gate_id: str


class HistoryInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    check_id: str
    limit: int = Field(50, ge=1, le=1000)


class HistoryPoint(BaseModel):
    ran_at: float = Field(json_schema_extra=ui(format="relative-time"))
    outcome: Outcome = Field(json_schema_extra=OUTCOME_UI)
    duration_s: float = Field(json_schema_extra=ui(format="duration"))
    tests: int | None
    failures: int | None
    sha: str | None = Field(json_schema_extra=ui(format="code"))
    dirty: bool


class CheckHistory(BaseModel):
    check_id: str
    points: list[HistoryPoint] = Field(
        json_schema_extra=ui(columns=["ran_at", "outcome", "duration_s", "tests", "sha"])
    )
    pass_rate: float | None = Field(None, json_schema_extra=ui(format="percent"))


class ReportInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    check_id: str


class LatestReport(BaseModel):
    run: CheckRunSummary
    report: JUnitReport | None
    log_tail: list[str]


class ScheduleInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    settings: CheckSettings


class CheckFinished(Event):
    type: ClassVar[str] = "checks.finished"
    persist: ClassVar[bool] = True
    check_id: str
    label: str
    outcome: Outcome
    tests: int | None
    failures: int | None

    def title(self) -> str:
        counts = f" ({self.failures} failing of {self.tests})" if self.tests is not None and self.failures else ""
        return f"Check {self.label}: {self.outcome}{counts}"

    def severity(self) -> Severity:
        return "success" if self.outcome == "passed" else "error"


class CheckRegressed(Event):
    type: ClassVar[str] = "checks.regressed"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "check-regression"
    check_id: str
    label: str

    def title(self) -> str:
        return f"Check {self.label} regressed from passing to failing"

    def severity(self) -> Severity:
        return "error"


def parse_gates(repo: Path) -> list[Gate]:
    """Hook ids and stages from `.pre-commit-config.yaml` (stages default to the config's `default_stages`)."""
    path = repo / ".pre-commit-config.yaml"
    if not path.is_file():
        return []
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return []
    if not isinstance(config, dict):
        return []
    default_stages = [str(stage) for stage in config.get("default_stages") or ["pre-commit"]]
    gates: list[Gate] = []
    for repo_entry in config.get("repos") or []:
        for hook in (repo_entry or {}).get("hooks") or []:
            if isinstance(hook, dict) and "id" in hook:
                stages = [str(stage) for stage in hook.get("stages") or default_stages]
                gates.append(Gate(id=str(hook["id"]), name=str(hook.get("name") or hook["id"]), stages=stages))
    return gates


def precommit_argv(repo: Path) -> list[str]:
    """How to invoke pre-commit for a repo: its own uv environment, PATH, or the dashboard's copy."""
    pyproject = repo / "pyproject.toml"
    if (repo / "uv.lock").is_file() and pyproject.is_file() and "pre-commit" in pyproject.read_text(encoding="utf-8"):
        return ["uv", "run", "pre-commit"]
    if shutil.which("pre-commit") is not None:
        return ["pre-commit"]
    return [sys.executable, "-m", "pre_commit"]


class ChecksService(Service[ChecksConfig, ChecksState], ProjectListener):
    id = "checks"
    title = "Checks"
    description = "Runs manifest checks and pre-commit gates, parses JUnit, tracks history, coverage, and staleness."
    Config = ChecksConfig
    State = ChecksState
    events = (CheckFinished, CheckRegressed)

    def __init__(
        self,
        runtime: Runtime,
        projects: ProjectsService,
        git: GitService,
        jobs: JobsService,
        scheduler: SchedulerService,
    ) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._git = git
        self._jobs = jobs
        self._scheduler = scheduler
        self._state: dict[str, ProjectChecks] = {}
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._background = BackgroundTasks()
        projects.listen(self)
        git.on_refreshed(lambda project_id, _state: self._publish(project_id))

    @override
    async def stop(self) -> None:
        for handle in self._pending.values():
            handle.cancel()
        await self._background.cancel_all()

    # --- registry hooks ---

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        settings = await self._load_settings(record.id)
        latest = await self._latest_runs(record)
        self._state[record.id] = ProjectChecks(
            project_id=record.id,
            checks=latest,
            gates=await asyncio.to_thread(parse_gates, record.path),
            coverage=await asyncio.to_thread(combined_coverage, record.path),
            settings=settings,
        )
        await self._sync_schedule(record, settings)
        self._publish(record.id)

    @override
    async def project_removed(self, project_id: str) -> None:
        self._state.pop(project_id, None)
        await self._scheduler.remove(f"checks:{project_id}")
        self._publish_state()

    @override
    async def files_changed(self, record: ProjectRecord, kinds: frozenset[ChangeKind]) -> None:
        project = self._state.get(record.id)
        if project is None:
            return
        if "worktree" in kinds:
            project.gates = await asyncio.to_thread(parse_gates, record.path)
            project.coverage = await asyncio.to_thread(combined_coverage, record.path)
        self._publish(record.id)
        schedule = project.settings.schedule
        if isinstance(schedule, OnChangeSchedule) and kinds & {"worktree", "git"} and record.manifest is not None:
            pending = self._pending.pop(record.id, None)
            if pending is not None:
                pending.cancel()
            self._pending[record.id] = asyncio.get_running_loop().call_later(
                schedule.debounce_s,
                lambda: self._background.spawn(self.run(RunChecksInput(project_id=record.id)), name="checks-onchange"),
            )

    async def _sync_schedule(self, record: ProjectRecord, settings: CheckSettings) -> None:
        entry_id = f"checks:{record.id}"
        if isinstance(settings.schedule, IntervalSchedule) and record.manifest is not None and record.manifest.checks:
            await self._scheduler.upsert(
                entry_id=entry_id,
                owner=self.id,
                title=f"Checks: {record.manifest.project.name}",
                project_id=record.id,
                target_service=self.id,
                target_action="run",
                payload={"project_id": record.id},
                schedule=settings.schedule,
            )
        elif self._scheduler.entry(entry_id) is not None:
            await self._scheduler.remove(entry_id)

    # --- state ---

    async def _load_settings(self, project_id: str) -> CheckSettings:
        stored = await self.runtime.db.kv_get("check-settings", project_id)
        return CheckSettings.model_validate(stored) if stored is not None else CheckSettings()

    async def _latest_runs(self, record: ProjectRecord) -> list[CheckRunSummary]:
        if record.manifest is None:
            return []
        summaries: list[CheckRunSummary] = []
        for check in record.manifest.checks:
            row = await self.runtime.db.fetch_one(
                "SELECT * FROM check_runs WHERE project_id = ? AND check_id = ? ORDER BY started_at DESC LIMIT 1",
                (record.id, check.id),
            )
            summaries.append(self._summary(check, row))
        return summaries

    @staticmethod
    def _summary(check: Check, row: sqlite3.Row | None) -> CheckRunSummary:
        base = CheckRunSummary(check_id=check.id, label=check.label, stage=check.stage)
        if row is None:
            return base
        return base.model_copy(
            update={
                "outcome": row["outcome"],
                "exit_code": row["exit_code"],
                "tests": row["tests"],
                "failures": row["failures"],
                "errors": row["errors"],
                "skipped": row["skipped"],
                "duration_s": row["duration_s"],
                "sha": row["sha"],
                "dirty": bool(row["dirty"]),
                "on_head": bool(row["on_head"]),
                "ran_at": row["started_at"] + row["duration_s"],
                "job_id": row["job_id"],
            }
        )

    def _staleness(self, project_id: str, run: CheckRunSummary) -> bool:
        repo = self._git.repo_state(project_id)
        if run.ran_at is None or repo is None:
            return False
        if run.sha is not None and repo.head is not None and run.sha != repo.head:
            return True
        changed_at = repo.last_worktree_change_at
        return not run.on_head and changed_at is not None and changed_at > run.ran_at

    def _publish(self, project_id: str) -> None:
        project = self._state.get(project_id)
        if project is None:
            return
        project.checks = [run.model_copy(update={"stale": self._staleness(project_id, run)}) for run in project.checks]
        self._projects.put(project_id, self._facts(project))
        self._publish_state()

    @staticmethod
    def _facts(project: ProjectChecks) -> CheckFacts:
        ran = [run for run in project.checks if run.outcome not in ("never", "running")]
        reported = [run for run in ran if run.tests is not None]
        latest = max(ran, key=lambda run: run.ran_at or 0, default=None)
        failed = sum(1 for run in ran if run.outcome != "passed")
        result: Literal["never-run", "running", "passing", "failing"]
        if any(run.outcome == "running" for run in project.checks):
            result = "running"
        elif not ran:
            result = "never-run"
        else:
            result = "failing" if failed else "passing"
        return CheckFacts(
            result=result,
            passed=len(ran) - failed,
            failed=failed,
            total=len(project.checks),
            tests_passed=sum((r.tests or 0) - (r.failures or 0) - (r.errors or 0) - (r.skipped or 0) for r in reported)
            if reported
            else None,
            tests_failed=sum((r.failures or 0) + (r.errors or 0) for r in reported) if reported else None,
            ran_at=latest.ran_at if latest is not None else None,
            duration_s=sum(run.duration_s or 0 for run in ran) if ran else None,
            sha=latest.sha[:7] if latest is not None and latest.sha else None,
            stale=any(run.stale for run in ran),
            coverage_percent=project.coverage.percent if project.coverage is not None else None,
        )

    def _publish_state(self) -> None:
        self.set_state(ChecksState(projects=sorted(self._state.values(), key=lambda project: project.project_id)))

    def project_checks(self, project_id: str) -> ProjectChecks | None:
        return self._state.get(project_id)

    # --- running ---

    async def run_checks_in(
        self,
        context: JobContext,
        record: ProjectRecord,
        checks: list[Check],
        root: Path,
        *,
        record_results: bool,
        on_head: bool,
        sha: str | None,
        dirty: bool,
    ) -> list[tuple[Check, Outcome]]:
        """Run checks with `root` as the project root (the repo or a worktree); used by syncs and bring-ups too."""
        results: list[tuple[Check, Outcome]] = []
        for index, check in enumerate(checks):
            context.progress(index / len(checks), check.label)
            outcome = await self._run_one(context, record, check, root, record_results, on_head, sha, dirty)
            results.append((check, outcome))
        return results

    async def _run_one(
        self,
        context: JobContext,
        record: ProjectRecord,
        check: Check,
        root: Path,
        record_results: bool,
        on_head: bool,
        sha: str | None,
        dirty: bool,
    ) -> Outcome:
        report_path = self.runtime.paths.jobs / f"{context.job_id}-{check.id}.junit.xml"
        command = render_command(
            argv=check.argv,
            cwd=check.cwd,
            env=check.env,
            project_root=root,
            project_id=record.id,
            dashboard_url=self.runtime.dashboard_url,
            values={"report_path": str(report_path)},
        )
        context.log(f"── {check.label} ({check.id})")
        started = time.time()
        result = await context.run(command.argv, command.cwd, command.env, timeout_s=check.timeout_s)
        duration = time.time() - started
        report: JUnitReport | None = None
        if check.report == "junit":
            try:
                report = read_junit(report_path)
            except JUnitError as error:
                context.log(f"junit: {error}")
        outcome: Outcome
        if result.timed_out:
            outcome = "timeout"
        elif result.ok and (report is None or not (report.failures or report.errors)):
            outcome = "passed"
        elif report is None and check.report == "junit":
            outcome = "error"
        else:
            outcome = "failed"
        context.log(f"── {check.label}: {outcome} in {duration:.1f}s")
        if record_results:
            await self._record(
                record, check, context.job_id, started, duration, outcome, result.exit_code, sha, dirty, on_head, report
            )
        return outcome

    async def _record(
        self,
        record: ProjectRecord,
        check: Check,
        job_id: str,
        started: float,
        duration: float,
        outcome: Outcome,
        exit_code: int | None,
        sha: str | None,
        dirty: bool,
        on_head: bool,
        report: JUnitReport | None,
    ) -> None:
        project = self._state.get(record.id)
        previous = await self.runtime.db.fetch_one(
            "SELECT outcome FROM check_runs WHERE project_id = ? AND check_id = ? ORDER BY started_at DESC LIMIT 1",
            (record.id, check.id),
        )
        await self.runtime.db.execute(
            "INSERT INTO check_runs (project_id, check_id, job_id, started_at, duration_s, outcome, exit_code, sha, "
            "dirty, on_head, tests, failures, errors, skipped, details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.id,
                check.id,
                job_id,
                started,
                duration,
                outcome,
                exit_code,
                sha,
                int(dirty),
                int(on_head),
                report.tests if report else None,
                report.failures if report else None,
                report.errors if report else None,
                report.skipped if report else None,
                report.model_dump_json() if report else "{}",
            ),
        )
        await self.runtime.db.execute(
            "DELETE FROM check_runs WHERE project_id = ? AND check_id = ? AND id NOT IN (SELECT id FROM check_runs "
            "WHERE project_id = ? AND check_id = ? ORDER BY started_at DESC LIMIT ?)",
            (record.id, check.id, record.id, check.id, self.config.keep_runs),
        )
        self.publish(
            CheckFinished(
                project_id=record.id,
                check_id=check.id,
                label=check.label,
                outcome=outcome,
                tests=report.tests if report else None,
                failures=(report.failures + report.errors) if report else None,
            )
        )
        if previous is not None and previous["outcome"] == "passed" and outcome != "passed":
            self.publish(CheckRegressed(project_id=record.id, check_id=check.id, label=check.label))
        if project is not None:
            row = await self.runtime.db.fetch_one(
                "SELECT * FROM check_runs WHERE project_id = ? AND check_id = ? ORDER BY started_at DESC LIMIT 1",
                (record.id, check.id),
            )
            project.checks = [self._summary(check, row) if run.check_id == check.id else run for run in project.checks]
            self._publish(record.id)

    def _mark_running(self, project_id: str, check_ids: set[str], job_id: str) -> None:
        project = self._state.get(project_id)
        if project is None:
            return
        project.checks = [
            run.model_copy(update={"outcome": "running", "job_id": job_id}) if run.check_id in check_ids else run
            for run in project.checks
        ]
        self._publish_state()

    # --- actions ---

    @action(title="Run checks", long_running=True)
    async def run(self, request: RunChecksInput) -> JobRef:
        """Run the project's manifest checks (or one of them) and record the results."""
        record = self._projects.record(request.project_id)
        if record.manifest is None or not record.manifest.checks:
            raise ActionError("this project's manifest declares no checks", status=409)
        checks = [c for c in record.manifest.checks if request.check_id in (None, c.id)]
        if not checks:
            raise ActionError(f"no check {request.check_id!r} in the manifest", status=404)
        settings = self._state[record.id].settings if record.id in self._state else CheckSettings()
        on_head = settings.on_head if request.on_head is None else request.on_head
        title = f"Checks: {checks[0].label}" if len(checks) == 1 else f"Checks ({len(checks)})"

        async def work(context: JobContext) -> JobOutcome:
            self._mark_running(record.id, {c.id for c in checks}, context.job_id)
            repo = await self._git.refresh(record) if record.is_git else None
            sha = repo.head if repo is not None else None
            if on_head and record.is_git and sha is not None:
                path = worktrees.worktree_path(
                    self.runtime.paths.worktrees, record.id, f"checks-{worktrees.timestamp()}"
                )
                async with worktrees.temporary(record.path, path, base=sha) as worktree:
                    results = await self.run_checks_in(
                        context, record, checks, worktree.path, record_results=True, on_head=True, sha=sha, dirty=False
                    )
            else:
                dirty = bool(repo.dirty) if repo is not None else False
                results = await self.run_checks_in(
                    context, record, checks, record.path, record_results=True, on_head=False, sha=sha, dirty=dirty
                )
            failed = [check.label for check, outcome in results if outcome != "passed"]
            if failed:
                return JobOutcome(status=JobStatus.FAILED, summary=f"failing: {', '.join(failed)}")
            return JobOutcome(summary=f"{len(results)} passed")

        return await self._jobs.submit(
            kind="checks.run", title=title, work=work, project_id=record.id, resources=["checks", f"repo:{record.id}"]
        )

    async def _gate_job(self, record: ProjectRecord, title: str, kind: str, args: list[str]) -> JobRef:
        async def work(context: JobContext) -> JobOutcome:
            argv = [*precommit_argv(record.path), *args]
            result = await context.run(argv, record.path, {"PRE_COMMIT_COLOR": "always"}, timeout_s=3600)
            if not result.ok:
                return JobOutcome(status=JobStatus.FAILED, summary=f"exit {result.exit_code}")
            return JobOutcome(summary="passed")

        return await self._jobs.submit(
            kind=kind, title=title, work=work, project_id=record.id, resources=["checks", f"repo:{record.id}"]
        )

    @action(title="Run gate", long_running=True)
    async def run_gate(self, request: GateInput) -> JobRef:
        """Run one pre-commit hook over all files (fixers may rewrite files, as in a commit)."""
        record = self._projects.record(request.project_id)
        gates = {gate.id: gate for gate in parse_gates(record.path)}
        gate = gates.get(request.gate_id)
        if gate is None:
            raise ActionError(f"no gate {request.gate_id!r} in .pre-commit-config.yaml", status=404)
        stage = ["--hook-stage", gate.stages[0]] if gate.stages and gate.stages[0] != "pre-commit" else []
        return await self._gate_job(record, f"Gate {gate.id}", "checks.gate", ["run", gate.id, "--all-files", *stage])

    @action(title="Run all commit gates", long_running=True)
    async def run_commit_gates(self, request: ProjectRef) -> JobRef:
        """`pre-commit run --all-files` (fixers may rewrite files, as in a commit)."""
        record = self._projects.record(request.project_id)
        if not (record.path / ".pre-commit-config.yaml").is_file():
            raise ActionError("this project has no .pre-commit-config.yaml", status=409)
        return await self._gate_job(record, "Commit gates", "checks.commit-gates", ["run", "--all-files"])

    @action(title="Run push gate", long_running=True)
    async def run_push_gate(self, request: ProjectRef) -> JobRef:
        """`pre-commit run --hook-stage pre-push --all-files`."""
        record = self._projects.record(request.project_id)
        if not (record.path / ".pre-commit-config.yaml").is_file():
            raise ActionError("this project has no .pre-commit-config.yaml", status=409)
        args = ["run", "--hook-stage", "pre-push", "--all-files"]
        return await self._gate_job(record, "Push gate", "checks.push-gate", args)

    @action(title="Check history", query=True)
    async def history(self, request: HistoryInput) -> CheckHistory:
        """Past runs of one check, oldest first, for pass-rate and duration charts."""
        rows = await self.runtime.db.fetch_all(
            "SELECT * FROM check_runs WHERE project_id = ? AND check_id = ? ORDER BY started_at DESC LIMIT ?",
            (request.project_id, request.check_id, request.limit),
        )
        points = [
            HistoryPoint(
                ran_at=row["started_at"],
                outcome=row["outcome"],
                duration_s=row["duration_s"],
                tests=row["tests"],
                failures=row["failures"],
                sha=row["sha"],
                dirty=bool(row["dirty"]),
            )
            for row in reversed(rows)
        ]
        rate = 100 * sum(1 for p in points if p.outcome == "passed") / len(points) if points else None
        return CheckHistory(check_id=request.check_id, points=points, pass_rate=rate)

    @action(title="Latest report", query=True)
    async def latest(self, request: ReportInput) -> LatestReport:
        """The latest run's JUnit details (failing tests and messages) and the end of its log."""
        record = self._projects.record(request.project_id)
        check = next((c for c in (record.manifest.checks if record.manifest else []) if c.id == request.check_id), None)
        if check is None:
            raise ActionError(f"no check {request.check_id!r}", status=404)
        row = await self.runtime.db.fetch_one(
            "SELECT * FROM check_runs WHERE project_id = ? AND check_id = ? ORDER BY started_at DESC LIMIT 1",
            (record.id, check.id),
        )
        summary = self._summary(check, row)
        details = json.loads(row["details"]) if row is not None else {}
        report = JUnitReport.model_validate(details) if details else None
        log_tail: list[str] = []
        if row is not None and row["job_id"]:
            job_log = self.runtime.paths.jobs / f"{row['job_id']}.log"
            if job_log.is_file():
                log_tail = job_log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        return LatestReport(run=summary, report=report, log_tail=log_tail)

    @action(title="Check schedule")
    async def set_settings(self, request: ScheduleInput) -> CheckSettings:
        """When this project's checks run: manual, on change, or on an interval; and whether on HEAD."""
        record = self._projects.record(request.project_id)
        await self.runtime.db.kv_put("check-settings", record.id, request.settings.model_dump(mode="json"))
        project = self._state.get(record.id)
        if project is not None:
            project.settings = request.settings
        await self._sync_schedule(record, request.settings)
        self._publish_state()
        return request.settings

    @action(title="Refresh gates and coverage")
    async def refresh(self, request: ProjectRef) -> Done:
        """Re-read the gate config and coverage reports."""
        await self.project_upserted(self._projects.record(request.project_id))
        return Done(message="refreshed")
