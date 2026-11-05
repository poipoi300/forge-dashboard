"""Manifest custom actions and the built-in "clean build artifacts" action.

A project declares `[[actions]]` in its manifest. This service lists them (with
their `params` JSON Schema, which the UI turns into a form) and runs them as
jobs: params are validated against the schema, written to the command's stdin as
one JSON document, and the exit code decides success. The command may describe
what it did in a JSON file named by `$FORGE_ACTION_RESULT`.

Cleaning deletes only directories that `artifact-sizes` reported for the same
project, never follows links, and never offers a directory holding tracked
files.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, override

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import AfterValidator, BaseModel, Field, JsonValue, ValidationError

from forge_dashboard import gitops
from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, JobRef, ProjectRef, Service, action
from forge_dashboard.core.ui import ui
from forge_dashboard.domain.commands import render_command
from forge_dashboard.manifest.models import Action as ManifestAction
from forge_dashboard.manifest.models import ActionResult
from forge_dashboard.process import tree
from forge_dashboard.services.jobs import JobContext, JobList, JobListInput, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

RUN_KIND = "actions.run"
CLEAN_KIND = "actions.clean"
JOB_KINDS = (RUN_KIND, CLEAN_KIND)
CLEAN_ACTION_ID = "clean-artifacts"
CLEAN_LABEL = "Clean build artifacts"
RESULT_ENV = "FORGE_ACTION_RESULT"

ARTIFACT_DIRS = (
    ".venv",
    "node_modules",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "coverage",
)
# Most artifact directories are looked for near the project root (monorepos nest them a level or two down);
# __pycache__ lives beside the sources, at any depth.
ARTIFACT_SEARCH_DEPTH = 3
UNBOUNDED_ARTIFACT_DIRS = frozenset({"__pycache__"})
_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")
_NEVER_ARTIFACTS = frozenset({"", ".", "..", ".git"})


def _check_directory_names(names: list[str]) -> list[str]:
    for name in names:
        if name in _NEVER_ARTIFACTS or "/" in name or "\\" in name:
            raise ValueError(f"{name!r}: give a plain directory name such as '.next' ('.git' is never offered)")
    return names


class ActionsConfig(BaseModel):
    extra_artifact_dirs: Annotated[list[str], AfterValidator(_check_directory_names)] = Field(
        default_factory=list,
        json_schema_extra=ui(
            widget="tags",
            group="Clean build artifacts",
            placeholder=".next",
            help="Directory names offered for cleaning besides the built-in ones (" + ", ".join(ARTIFACT_DIRS) + ").",
        ),
    )


class ActionInfo(BaseModel):
    """A manifest action as the UI needs it to build a form."""

    id: str = Field(json_schema_extra=ui(format="code"))
    label: str
    description: str | None = None
    params: dict[str, JsonValue] | None = Field(
        None, json_schema_extra=ui(widget="json", help="JSON Schema of the input form.")
    )
    confirm: bool = False
    danger: bool = False
    requires_service: str | None = Field(None, json_schema_extra=ui(short_label="needs service"))


class ProjectActions(BaseModel):
    project_id: str
    actions: list[ActionInfo] = Field(
        json_schema_extra=ui(columns=["label", "description", "requires_service", "danger"])
    )


class ActionsState(BaseModel):
    projects: list[ProjectActions] = Field(default_factory=list, json_schema_extra=ui(help="Projects with actions."))


class RunActionInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    action_id: str = Field(json_schema_extra=ui(hidden=True))
    params: dict[str, JsonValue] = Field(
        default_factory=dict, json_schema_extra=ui(widget="json", help="Checked against the action's params schema.")
    )


class ActionRunResult(BaseModel):
    """The result of a finished action job."""

    action_id: str
    label: str
    exit_code: int | None
    timed_out: bool
    result: ActionResult | None = None
    result_error: str | None = Field(None, json_schema_extra=ui(help="Why the result file was ignored."))


class Artifact(BaseModel):
    path: str = Field(json_schema_extra=ui(format="path", help="Relative to the project root."))
    kind: str = Field(json_schema_extra=ui(format="badge"))
    size_bytes: int = Field(json_schema_extra=ui(format="bytes", short_label="size"))


class ArtifactSizes(BaseModel):
    project_id: str
    total_bytes: int = Field(json_schema_extra=ui(format="bytes"))
    artifacts: list[Artifact] = Field(json_schema_extra=ui(columns=["path", "kind", "size_bytes"]))


class CleanInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    paths: list[str] = Field(
        min_length=1, json_schema_extra=ui(widget="multiselect", help="Paths reported by Artifact sizes.")
    )


class CleanResult(BaseModel):
    freed_bytes: int = Field(json_schema_extra=ui(format="bytes"))
    removed: list[str]


class HistoryInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    limit: int = Field(50, ge=1, le=500)


class ActionRan(Event):
    type: ClassVar[str] = "actions.ran"
    persist: ClassVar[bool] = True
    action_id: str
    label: str
    job_id: str
    succeeded: bool
    summary: str

    def title(self) -> str:
        return f"Ran {self.label}: {self.summary}"

    def severity(self) -> Severity:
        return "success" if self.succeeded else "error"

    def dedupe_key(self) -> str:
        return f"actions:{self.job_id}"


# --- artifacts on disk (blocking; run in a thread) ---


def find_artifact_dirs(root: Path, names: Iterable[str]) -> list[Path]:
    """Directories under `root` named like build output; links and `.git` are never entered or reported."""
    wanted = frozenset(names)
    found: list[Path] = []
    pending = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == ".git" or entry.is_symlink() or entry.is_junction():
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                child = Path(entry.path)
                if entry.name in wanted and (depth < ARTIFACT_SEARCH_DEPTH or entry.name in UNBOUNDED_ARTIFACT_DIRS):
                    found.append(child)
                else:
                    pending.append((child, depth + 1))
    return found


def directory_size(directory: Path) -> int:
    """Total bytes of the files under `directory`, counting no link and following none."""
    total = 0
    pending = [directory]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_size
    return total


def _make_writable_and_retry(function: Callable[..., Any], path: str, _error: BaseException) -> None:
    """Read-only files (git objects, some node_modules) block deletion on Windows until made writable."""
    Path(path).chmod(stat.S_IWRITE)
    function(path)


def remove_tree(directory: Path) -> None:
    shutil.rmtree(directory, onexc=_make_writable_and_retry)


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in _BYTE_UNITS[:-1]:
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {_BYTE_UNITS[-1]}"


def artifact_target(root: Path, relative: str) -> Path:
    """The directory to delete for a reported path.

    Raises:
        ActionError: when it has become a link, or does not lie inside `root`.
    """
    target = root / relative
    if target.is_symlink() or target.is_junction():
        raise ActionError(f"{relative} is a link now; refusing to delete through it", status=409)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ActionError(f"{relative} lies outside the project", status=422)
    return target


def _schema_problems(schema: dict[str, JsonValue], params: dict[str, JsonValue]) -> list[str]:
    errors: list[SchemaValidationError] = sorted(
        Draft202012Validator(schema).iter_errors(params), key=lambda error: [str(part) for part in error.absolute_path]
    )
    return [f"{'.'.join(str(part) for part in error.absolute_path) or 'params'}: {error.message}" for error in errors]


def _result_problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in problem['loc']) or 'document'}: {problem['msg']}"
        for problem in error.errors(include_url=False, include_context=False, include_input=False)
    )


class ActionsService(Service[ActionsConfig, ActionsState], ProjectListener):
    id = "actions"
    title = "Actions"
    description = "Runs manifest custom actions with schema-validated params, and cleans build artifacts."
    Config = ActionsConfig
    State = ActionsState
    events = (ActionRan,)

    def __init__(
        self,
        runtime: Runtime,
        projects: ProjectsService,
        jobs: JobsService,
        service_url: Callable[[str, str], str | None],
    ) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._jobs = jobs
        self._service_url = service_url
        self._catalog: dict[str, ProjectActions] = {}
        projects.listen(self)

    # --- registry hooks ---

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        declared = record.manifest.actions if record.manifest is not None else []
        if declared:
            self._catalog[record.id] = ProjectActions(
                project_id=record.id,
                actions=[
                    ActionInfo(
                        id=declared_action.id,
                        label=declared_action.label,
                        description=declared_action.description,
                        params=declared_action.params,
                        confirm=declared_action.confirm,
                        danger=declared_action.danger,
                        requires_service=declared_action.requires_service,
                    )
                    for declared_action in declared
                ],
            )
        else:
            self._catalog.pop(record.id, None)
        self._publish_catalog()

    @override
    async def project_removed(self, project_id: str) -> None:
        self._catalog.pop(project_id, None)
        self._publish_catalog()

    def _publish_catalog(self) -> None:
        self.set_state(ActionsState(projects=sorted(self._catalog.values(), key=lambda entry: entry.project_id)))

    # --- custom actions ---

    @staticmethod
    def _declared_action(record: ProjectRecord, action_id: str) -> ManifestAction:
        if record.manifest is None:
            raise ActionError(f"{record.id} has no valid manifest, so it declares no actions", status=409)
        for declared in record.manifest.actions:
            if declared.id == action_id:
                return declared
        raise ActionError(f"no action {action_id!r} in {record.id}'s manifest", status=404)

    @staticmethod
    def _check_params(declared: ManifestAction, params: dict[str, JsonValue]) -> None:
        if declared.params is None:
            if params:
                raise ActionError(f"action {declared.id!r} takes no params", status=422)
            return
        problems = _schema_problems(declared.params, params)
        if problems:
            raise ActionError(f"invalid params for {declared.id!r}: {'; '.join(problems)}", status=422)

    def _service_env(self, record: ProjectRecord, declared: ManifestAction) -> dict[str, str]:
        if declared.requires_service is None:
            return {}
        url = self._service_url(record.id, declared.requires_service)
        if url is None:
            raise ActionError(
                f"action {declared.id!r} needs the service {declared.requires_service!r} to be running; start it first",
                status=409,
            )
        return {f"FORGE_SERVICE_URL_{declared.requires_service.upper().replace('-', '_')}": url}

    @action(title="Run action", long_running=True)
    async def run(self, request: RunActionInput) -> JobRef:
        """Run a manifest action with params checked against its schema; they reach it as JSON on stdin."""
        record = self._projects.record(request.project_id)
        declared = self._declared_action(record, request.action_id)
        self._check_params(declared, request.params)
        service_env = self._service_env(record, declared)

        async def work(context: JobContext) -> JobOutcome:
            return await self._run_declared(context, record, declared, request.params, service_env)

        return await self._jobs.submit(
            kind=RUN_KIND,
            title=f"Action: {declared.label}",
            work=work,
            project_id=record.id,
            resources=[f"repo:{record.id}"],
        )

    async def _run_declared(
        self,
        context: JobContext,
        record: ProjectRecord,
        declared: ManifestAction,
        params: dict[str, JsonValue],
        service_env: dict[str, str],
    ) -> JobOutcome:
        result_path = self.runtime.paths.jobs / f"{context.job_id}.action-result.json"
        command = render_command(
            argv=declared.argv,
            cwd=declared.cwd,
            env=declared.env,
            project_root=record.path,
            project_id=record.id,
            dashboard_url=self.runtime.dashboard_url,
            extra_env={**service_env, RESULT_ENV: str(result_path)},
        )
        stdin = json.dumps(params).encode("utf-8") if declared.params is not None else None
        try:
            completed = await context.run(
                command.argv, command.cwd, command.env, timeout_s=declared.timeout_s, stdin=stdin
            )
        except tree.ExecutableNotFoundError as error:
            context.log(f"error: {error}")
            return self._conclude(
                context, record, declared.id, declared.label, JobOutcome(JobStatus.FAILED, str(error))
            )
        result: ActionResult | None = None
        result_error: str | None = None
        if result_path.is_file():
            try:
                result = ActionResult.model_validate_json(result_path.read_bytes())
            except ValidationError as error:
                result_error = _result_problems(error)
                context.log(f"result file ignored: {result_error}")
        if completed.timed_out:
            summary = f"timed out after {declared.timeout_s}s"
        elif not completed.ok:
            summary = f"exit {completed.exit_code}"
        else:
            summary = "done"
        if result is not None and result.summary:
            summary = result.summary if completed.ok else f"{summary}: {result.summary}"
        outcome = JobOutcome(
            status=JobStatus.SUCCEEDED if completed.ok else JobStatus.FAILED,
            summary=summary,
            result=ActionRunResult(
                action_id=declared.id,
                label=declared.label,
                exit_code=completed.exit_code,
                timed_out=completed.timed_out,
                result=result,
                result_error=result_error,
            ),
        )
        return self._conclude(context, record, declared.id, declared.label, outcome)

    def _conclude(
        self, context: JobContext, record: ProjectRecord, action_id: str, label: str, outcome: JobOutcome
    ) -> JobOutcome:
        self.publish(
            ActionRan(
                project_id=record.id,
                action_id=action_id,
                label=label,
                job_id=context.job_id,
                succeeded=outcome.status is JobStatus.SUCCEEDED,
                summary=outcome.summary or outcome.status.value,
            )
        )
        return outcome

    # --- built-in: clean build artifacts ---

    async def _artifacts(self, record: ProjectRecord) -> list[Artifact]:
        names = (*ARTIFACT_DIRS, *self.config.extra_artifact_dirs)
        found = await asyncio.to_thread(find_artifact_dirs, record.path, names)
        if record.is_git and found:
            tracked = await self._holding_tracked_files(record, found)
            found = [directory for directory in found if directory not in tracked]
        sizes = await asyncio.to_thread(lambda: [directory_size(directory) for directory in found])
        artifacts = [
            Artifact(path=directory.relative_to(record.path).as_posix(), kind=directory.name, size_bytes=size)
            for directory, size in zip(found, sizes, strict=True)
        ]
        return sorted(artifacts, key=lambda artifact: (-artifact.size_bytes, artifact.path))

    @staticmethod
    async def _holding_tracked_files(record: ProjectRecord, directories: list[Path]) -> set[Path]:
        """Candidates containing files git tracks: deleting them would show up as changes in the working tree."""
        listing = await gitops.run(record.path, "ls-files", "-z")
        tracked_directories = {
            parent.as_posix() for file in listing.stdout.split("\0") if file for parent in PurePosixPath(file).parents
        }
        return {
            directory
            for directory in directories
            if directory.relative_to(record.path).as_posix() in tracked_directories
        }

    @action(title="Artifact sizes", query=True)
    async def artifact_sizes(self, request: ProjectRef) -> ArtifactSizes:
        """Sizes of the project's build output and caches (.venv, node_modules, target, caches, dist, ...)."""
        record = self._projects.record(request.project_id)
        artifacts = await self._artifacts(record)
        return ArtifactSizes(
            project_id=record.id, total_bytes=sum(artifact.size_bytes for artifact in artifacts), artifacts=artifacts
        )

    @action(title=CLEAN_LABEL, long_running=True, danger=True, confirm=True)
    async def clean_artifacts(self, request: CleanInput) -> JobRef:
        """Delete build artifact directories; only paths that Artifact sizes reports for this project are accepted."""
        record = self._projects.record(request.project_id)
        reported = {artifact.path: artifact for artifact in await self._artifacts(record)}
        unknown = [path for path in request.paths if path not in reported]
        if unknown:
            raise ActionError(f"not reported by artifact sizes for {record.id}: {', '.join(unknown)}", status=422)
        chosen = [reported[path] for path in dict.fromkeys(request.paths)]
        for artifact in chosen:
            artifact_target(record.path, artifact.path)

        async def work(context: JobContext) -> JobOutcome:
            freed = 0
            removed: list[str] = []
            for index, artifact in enumerate(chosen):
                context.progress(index / len(chosen), artifact.path)
                context.log(f"removing {artifact.path} ({human_bytes(artifact.size_bytes)})")
                try:
                    await asyncio.to_thread(remove_tree, artifact_target(record.path, artifact.path))
                except OSError as error:
                    context.log(f"error: {error}")
                    outcome = JobOutcome(
                        JobStatus.FAILED,
                        f"could not remove {artifact.path}: {error.strerror or error}",
                        CleanResult(freed_bytes=freed, removed=removed),
                    )
                    return self._conclude(context, record, CLEAN_ACTION_ID, CLEAN_LABEL, outcome)
                freed += artifact.size_bytes
                removed.append(artifact.path)
            summary = f"freed {human_bytes(freed)} from {len(removed)} directories"
            outcome = JobOutcome(summary=summary, result=CleanResult(freed_bytes=freed, removed=removed))
            return self._conclude(context, record, CLEAN_ACTION_ID, CLEAN_LABEL, outcome)

        return await self._jobs.submit(
            kind=CLEAN_KIND, title=CLEAN_LABEL, work=work, project_id=record.id, resources=[f"repo:{record.id}"]
        )

    # --- history ---

    @action(title="Action history", query=True)
    async def history(self, request: HistoryInput) -> JobList:
        """The project's action jobs (custom actions and cleaning), newest first."""
        pages = [
            await self._jobs.history(JobListInput(project_id=request.project_id, kind=kind, limit=request.limit))
            for kind in JOB_KINDS
        ]
        jobs = sorted(
            (job for page in pages for job in page.jobs), key=lambda job: (job.created_at, job.id), reverse=True
        )
        return JobList(jobs=jobs[: request.limit])
