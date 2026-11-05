"""forge's agent runs: per-project step trajectories with their stdout and stderr.

forge's runner (see `src/forge/runner.py` in the forge repo) writes, for every
agent invocation, `<workspace_root>/<project>/meta/step-NN-<name>.json` plus
`.stdout.txt` and `.stderr.txt` next to it. `workspace_root` is read live from
the forge repo's `forge.toml`. Directories beside the forge repo that follow
the same layout, and any configured extra roots, are shown too, so a run kept
elsewhere (`Tests/forge-runs/hair-brawler/meta/`) needs no setup.

State carries summaries only: a step's command is cut to a one-line digest
because it embeds the whole prompt. The query actions return the full
manifest and the output lines of one step, and refuse any path that does not
lead into a known run root.
"""

from __future__ import annotations

import asyncio
import time
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, override

from pydantic import BaseModel, Field, ValidationError, field_validator

from forge_dashboard.core.service import ActionError, NoInput, Service, action
from forge_dashboard.core.tasks import BackgroundTasks, repeat
from forge_dashboard.core.ui import ui
from forge_dashboard.services.projects import ProjectListener

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime
    from forge_dashboard.services.projects import ProjectRecord, ProjectsService

FORGE_CONFIG_FILENAME = "forge.toml"
META_DIRECTORY = "meta"
STEP_MANIFEST_GLOB = "step-*.json"
LONGEST_ARGUMENT_CHARS = 48
LONGEST_COMMAND_CHARS = 200
MAX_OUTPUT_LINES = 5000

RunRootOrigin = Literal["forge-toml", "configured", "sibling"]
OutputStream = Literal["stdout", "stderr"]
_ORIGIN_ORDER: dict[RunRootOrigin, int] = {"forge-toml": 0, "configured": 1, "sibling": 2}

OK_UI = ui(format="badge", enum_labels={"true": "ok", "false": "failed"}, tones={"true": "good", "false": "bad"})
TIMED_OUT_UI = ui(
    format="badge",
    short_label="timeout",
    enum_labels={"true": "timed out", "false": "no"},
    tones={"true": "bad", "false": "neutral"},
)
ORIGIN_UI = ui(
    format="badge",
    enum_labels={"forge-toml": "forge.toml", "configured": "configured", "sibling": "beside forge"},
    tones={"forge-toml": "info", "configured": "neutral", "sibling": "neutral"},
)


class StepManifest(BaseModel):
    """One step's manifest exactly as forge's runner wrote it (`StepResult`)."""

    project: str
    step_index: int
    step_name: str
    prompt_file: str = Field(json_schema_extra=ui(format="path"))
    harness: str
    command: list[str] = Field(json_schema_extra=ui(help="The full argv, prompt text included."))
    workspace: str = Field(json_schema_extra=ui(format="path"))
    started_at: datetime
    duration_s: float = Field(json_schema_extra=ui(format="duration"))
    exit_code: int | None
    timed_out: bool = Field(json_schema_extra=TIMED_OUT_UI)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def summarize_command(argv: Sequence[str]) -> str:
    """argv[0] and the flags, with every long or multi-line argument (the prompt) replaced by its length."""
    if not argv:
        return ""
    parts = [argv[0]] + [
        f"<{len(argument)} chars>" if len(argument) > LONGEST_ARGUMENT_CHARS or "\n" in argument else argument
        for argument in argv[1:]
    ]
    summary = " ".join(parts)
    return summary if len(summary) <= LONGEST_COMMAND_CHARS else summary[: LONGEST_COMMAND_CHARS - 1] + "…"


class StepSummary(BaseModel):
    stem: str = Field(
        json_schema_extra=ui(format="code", help="File name without extension; names the step in actions.")
    )
    index: int
    name: str
    ok: bool = Field(json_schema_extra=OK_UI)
    exit_code: int | None = None
    timed_out: bool = Field(json_schema_extra=TIMED_OUT_UI)
    started_at: float = Field(json_schema_extra=ui(format="relative-time"))
    duration_s: float = Field(json_schema_extra=ui(format="duration", short_label="duration"))
    harness: str
    command: str = Field(json_schema_extra=ui(format="code", help="argv[0] and flags; the prompt is elided."))
    workspace: str = Field(json_schema_extra=ui(format="path"))
    workspace_exists: bool = Field(json_schema_extra=ui(help="Whether the workspace folder is still on disk."))
    stdout_bytes: int | None = Field(None, json_schema_extra=ui(format="bytes", help="Unset when there is no file."))
    stderr_bytes: int | None = Field(None, json_schema_extra=ui(format="bytes", help="Unset when there is no file."))


class UnreadableStep(BaseModel):
    stem: str = Field(json_schema_extra=ui(format="code"))
    problem: str


class ForgeRun(BaseModel):
    """One project's trajectory under a run root."""

    name: str
    root: str = Field(json_schema_extra=ui(format="path"))
    steps: list[StepSummary] = Field(
        default_factory=list,
        json_schema_extra=ui(
            columns=["index", "name", "ok", "exit_code", "timed_out", "started_at", "duration_s", "harness"]
        ),
    )
    unreadable: list[UnreadableStep] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["stem", "problem"], help="Manifests that are not valid step manifests."),
    )
    total_duration_s: float = Field(0.0, json_schema_extra=ui(format="duration", short_label="total"))
    last_activity: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="last step"))


class RunRoot(BaseModel):
    path: str = Field(json_schema_extra=ui(format="path"))
    origin: RunRootOrigin = Field(json_schema_extra=ORIGIN_UI)
    exists: bool = Field(json_schema_extra=ui(help="A configured root can be missing before forge first runs."))
    projects: int = Field(0, json_schema_extra=ui(short_label="runs"))


class ForgeRunsConfig(BaseModel):
    interval_s: int = Field(
        60, ge=1, le=3600, json_schema_extra=ui(widget="duration", help="How often run folders are read again.")
    )
    extra_roots: list[str] = Field(
        default_factory=list,
        json_schema_extra=ui(
            widget="path-dir", help="Folders laid out like forge's workspace root: <root>/<project>/meta/step-*.json."
        ),
    )

    @field_validator("extra_roots")
    @classmethod
    def _absolute(cls, roots: list[str]) -> list[str]:
        relative = [root for root in roots if not Path(root).is_absolute()]
        if relative:
            raise ValueError(f"run roots must be absolute paths, got {relative[0]!r}")
        return roots


class ForgeRunsState(BaseModel):
    forge_repo: str | None = Field(None, json_schema_extra=ui(format="path"))
    notice: str | None = Field(None, json_schema_extra=ui(help="Why forge's own workspace root is not being read."))
    roots: list[RunRoot] = Field(default_factory=list, json_schema_extra=ui(columns=["path", "origin", "projects"]))
    runs: list[ForgeRun] = Field(
        default_factory=list, json_schema_extra=ui(columns=["name", "root", "total_duration_s", "last_activity"])
    )
    refreshed_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))


class StepRef(BaseModel):
    root: str = Field(json_schema_extra=ui(hidden=True, help="A run root listed in the state."))
    project: str = Field(json_schema_extra=ui(hidden=True))
    step: str = Field(json_schema_extra=ui(hidden=True, help="The step's file stem, e.g. step-00-initial."))


class StepOutputRequest(StepRef):
    which: OutputStream = Field("stdout", json_schema_extra=ui(widget="select"))
    offset: int = Field(0, ge=0, json_schema_extra=ui(help="First line to return, counting from 0."))
    limit: int = Field(500, ge=1, le=MAX_OUTPUT_LINES, json_schema_extra=ui(help="Most lines to return."))


class StepOutput(BaseModel):
    which: OutputStream
    lines: list[str]
    offset: int
    total_lines: int
    size_bytes: int = Field(json_schema_extra=ui(format="bytes"))


# --- reading run folders (blocking; run in a worker thread) ---


class ForgeTomlError(Exception):
    """forge.toml cannot tell where runs go."""


def workspace_root(forge_repo: Path) -> Path:
    """`[runner].workspace_root` from forge.toml, relative to the forge repo (an absolute path stands as written).

    Raises:
        ForgeTomlError: when forge.toml is missing, is not valid TOML, or has no usable `[runner]` table.
    """
    config_path = forge_repo / FORGE_CONFIG_FILENAME
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ForgeTomlError(f"{config_path} not found") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ForgeTomlError(f"{config_path} is not valid TOML: {error}") from error
    runner = config.get("runner")
    if not isinstance(runner, dict):
        raise ForgeTomlError(f"{config_path} has no [runner] table")
    configured = runner.get("workspace_root")
    if not isinstance(configured, str) or not configured:
        raise ForgeTomlError(f"{config_path}: [runner] workspace_root must be a non-empty string")
    return (forge_repo / configured).resolve()


def _holds_step_manifests(project_dir: Path) -> bool:
    try:
        return any((project_dir / META_DIRECTORY).glob(STEP_MANIFEST_GLOB))
    except (NotADirectoryError, PermissionError):
        return False


def run_project_dirs(root: Path) -> list[Path]:
    """The children of `root` laid out like a forge run: `<project>/meta/step-*.json`, by name.

    A folder this process cannot list holds no runs it could show.
    """
    try:
        children = sorted(root.iterdir(), key=lambda child: child.name)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []
    return [child for child in children if _holds_step_manifests(child)]


def sibling_run_roots(forge_repo: Path) -> list[Path]:
    """Directories next to the forge repo that hold at least one run."""
    own = forge_repo.resolve()
    return sorted(
        (
            resolved
            for sibling in forge_repo.parent.iterdir()
            if (resolved := sibling.resolve()) != own and run_project_dirs(resolved)
        ),
        key=str,
    )


@dataclass(frozen=True)
class RunRootLocation:
    """Where runs are looked for, and why forge's own workspace root is not among them if it is not."""

    roots: dict[Path, RunRootOrigin]
    notice: str | None


def locate_run_roots(forge_repo: Path | None, extra_roots: Iterable[str]) -> RunRootLocation:
    """forge.toml's workspace root, the configured roots, then sibling directories of the forge repo.

    A directory reachable more than one way keeps its first origin in that order.
    """
    roots: dict[Path, RunRootOrigin] = {}
    notice: str | None = None
    if forge_repo is None:
        notice = "The forge repo was not found, so its workspace root is unknown. Set it under Discovery."
    else:
        try:
            roots[workspace_root(forge_repo)] = "forge-toml"
        except ForgeTomlError as error:
            notice = str(error)
    for extra in extra_roots:
        roots.setdefault(Path(extra).resolve(), "configured")
    if forge_repo is not None:
        for sibling in sibling_run_roots(forge_repo):
            roots.setdefault(sibling, "sibling")
    return RunRootLocation(roots, notice)


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


def _validation_problem(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    return f"{location}: {first['msg']}" if location else str(first["msg"])


def _summarize_step(manifest: StepManifest, stem: str, meta: Path) -> StepSummary:
    return StepSummary(
        stem=stem,
        index=manifest.step_index,
        name=manifest.step_name,
        ok=manifest.ok,
        exit_code=manifest.exit_code,
        timed_out=manifest.timed_out,
        started_at=manifest.started_at.timestamp(),
        duration_s=manifest.duration_s,
        harness=manifest.harness,
        command=summarize_command(manifest.command),
        workspace=manifest.workspace,
        workspace_exists=Path(manifest.workspace).is_dir(),
        stdout_bytes=_file_size(meta / f"{stem}.stdout.txt"),
        stderr_bytes=_file_size(meta / f"{stem}.stderr.txt"),
    )


def read_run(root: Path, project_dir: Path) -> ForgeRun:
    """Every step manifest of one project, in run order: step index, then start time, then file name."""
    meta = project_dir / META_DIRECTORY
    steps: list[StepSummary] = []
    unreadable: list[UnreadableStep] = []
    for manifest_path in sorted(meta.glob(STEP_MANIFEST_GLOB)):
        try:
            manifest = StepManifest.model_validate_json(manifest_path.read_bytes())
        except ValidationError as error:
            unreadable.append(UnreadableStep(stem=manifest_path.stem, problem=_validation_problem(error)))
            continue
        steps.append(_summarize_step(manifest, manifest_path.stem, meta))
    steps.sort(key=lambda step: (step.index, step.started_at, step.stem))
    return ForgeRun(
        name=project_dir.name,
        root=str(root),
        steps=steps,
        unreadable=unreadable,
        total_duration_s=sum(step.duration_s for step in steps),
        last_activity=max((step.started_at + step.duration_s for step in steps), default=None),
    )


def scan_runs(forge_repo: Path | None, extra_roots: Iterable[str]) -> ForgeRunsState:
    """Locate the run roots and read every run in them."""
    location = locate_run_roots(forge_repo, extra_roots)
    roots: list[RunRoot] = []
    runs: list[ForgeRun] = []
    for root, origin in location.roots.items():
        found = [read_run(root, project_dir) for project_dir in run_project_dirs(root)]
        runs.extend(found)
        roots.append(RunRoot(path=str(root), origin=origin, exists=root.is_dir(), projects=len(found)))
    roots.sort(key=lambda root: (_ORIGIN_ORDER[root.origin], root.path))
    runs.sort(key=lambda run: (run.last_activity is None, -(run.last_activity or 0.0), run.root, run.name))
    return ForgeRunsState(
        forge_repo=str(forge_repo) if forge_repo is not None else None,
        notice=location.notice,
        roots=roots,
        runs=runs,
        refreshed_at=time.time(),
    )


def read_output_lines(path: Path, which: OutputStream, offset: int, limit: int) -> StepOutput:
    """`limit` lines of a captured stream from line `offset`; the rest of the file is only counted."""
    window: list[str] = []
    total = 0
    with path.open("rb") as handle:
        for raw in handle:
            if offset <= total < offset + limit:
                window.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            total += 1
    return StepOutput(which=which, lines=window, offset=offset, total_lines=total, size_bytes=path.stat().st_size)


# --- the service ---


def _component(value: str, label: str) -> str:
    """A single path component: no separators, no drive, no parent references."""
    if not value or value in {".", ".."} or Path(value).name != value or ":" in value or "\x00" in value:
        raise ActionError(f"invalid {label} {value!r}", status=400)
    return value


class ForgeRunsService(Service[ForgeRunsConfig, ForgeRunsState], ProjectListener):
    id = "forge_runs"
    title = "Forge runs"
    description = "forge's agent runs: steps per project with exit codes, timings, and captured output."
    Config = ForgeRunsConfig
    State = ForgeRunsState

    def __init__(self, runtime: Runtime, projects: ProjectsService) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._background = BackgroundTasks()
        self._refreshing = asyncio.Lock()
        projects.listen(self)

    @override
    async def start(self) -> None:
        self._background.spawn(
            repeat(self._refresh, lambda: self.config.interval_s, label="forge runs refresh"), "forge-runs"
        )

    @override
    async def stop(self) -> None:
        await self._background.cancel_all()

    @override
    async def on_config_changed(self, previous: ForgeRunsConfig) -> None:
        await self._refresh()

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        # Discovery learns where the forge repo is only after start.
        if record.role == "standards-source":
            self._background.spawn(self._refresh(), "forge-runs-forge-repo-found")

    async def _refresh(self) -> None:
        async with self._refreshing:
            self.set_state(
                await asyncio.to_thread(scan_runs, self._projects.forge_repo, tuple(self.config.extra_roots))
            )

    def _run_root(self, requested: str) -> Path:
        """The known run root `requested` names; anything else is refused."""
        resolved = Path(requested).resolve()
        if not any(root.exists and Path(root.path) == resolved for root in self.state.roots):
            raise ActionError(f"{requested!r} is not a known run root", status=404)
        return resolved

    def _meta_file(self, request: StepRef, suffix: str) -> Path:
        """`<root>/<project>/meta/<step><suffix>`, which must exist and lie inside the run root."""
        base = self._run_root(request.root)
        meta = base / _component(request.project, "project") / META_DIRECTORY
        target = (meta / f"{_component(request.step, 'step')}{suffix}").resolve()
        if not target.is_relative_to(base):
            raise ActionError("path leaves the run root", status=400)
        if not target.is_file():
            raise ActionError(f"no such file: {target.name}", status=404)
        return target

    @action(title="Refresh runs")
    async def refresh(self, _request: NoInput) -> ForgeRunsState:
        """Read forge.toml and every run folder again."""
        await self._refresh()
        return self.state

    @action(title="Step manifest", query=True)
    async def step_manifest(self, request: StepRef) -> StepManifest:
        """The full manifest of one step, command and prompt text included."""
        path = self._meta_file(request, ".json")
        try:
            return StepManifest.model_validate_json(await asyncio.to_thread(path.read_bytes))
        except ValidationError as error:
            raise ActionError(
                f"{path.name} is not a step manifest: {_validation_problem(error)}", status=422
            ) from error

    @action(title="Step output", query=True)
    async def step_output(self, request: StepOutputRequest) -> StepOutput:
        """A window of lines from one step's captured stdout or stderr."""
        path = self._meta_file(request, f".{request.which}.txt")
        return await asyncio.to_thread(read_output_lines, path, request.which, request.offset, request.limit)
