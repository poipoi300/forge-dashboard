"""Opening projects in the user's tools, backup and restore of dashboard state, and report export.

Editor, terminal, and file-manager launchers are argv templates with a `{path}`
placeholder. The first start detects what is installed and stores the result,
so the settings page shows real values the user can edit. A project may
override the editor (`ProjectSettings.editor_command`).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, override

from pydantic import AfterValidator, BaseModel, Field, TypeAdapter

from forge_dashboard.core.service import ActionError, NoInput, Service, action
from forge_dashboard.core.ui import ui
from forge_dashboard.domain.report import render_markdown
from forge_dashboard.domain.summary import ProjectSummary
from forge_dashboard.manifest import placeholders as ph
from forge_dashboard.process import tree
from forge_dashboard.services.projects import ProjectRecord, ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

PATH_PLACEHOLDER = "path"
BACKUP_SUFFIX = ".sqlite3"
PRE_RESTORE_PREFIX = "pre-restore-"

ToolKind = Literal["editor", "terminal", "file-manager"]
ReportFormat = Literal["markdown", "json"]
REPORT_EXTENSIONS: dict[ReportFormat, str] = {"markdown": "md", "json": "json"}


# --- launcher templates ---


def template_problems(template: Sequence[str]) -> list[str]:
    """Why `template` cannot be rendered as a launch command; empty when it can."""
    if not template:
        return []
    problems = [] if template[0] else ["the first element must name an executable"]
    problems.extend(ph.check_placeholders(template, [PATH_PLACEHOLDER])[1])
    return problems


def _check_template(template: list[str]) -> list[str]:
    problems = template_problems(template)
    if problems:
        raise ValueError("; ".join(problems))
    return template


LauncherTemplate = Annotated[list[str], AfterValidator(_check_template)]


def render_launch(template: Sequence[str], target: Path) -> list[str]:
    """The command that opens `target` with `template`.

    Raises:
        ActionError: 422 when the template is malformed or uses a placeholder other than `{path}`.
    """
    problems = template_problems(template)
    if problems:
        raise ActionError(f"launcher {list(template)}: {'; '.join(problems)}", status=422)
    return [ph.render(part, {PATH_PLACEHOLDER: str(target)}) for part in template]


def _launcher_field(placeholder: str, help_text: str, order: int) -> Any:
    return Field(
        default_factory=list,
        json_schema_extra=ui(widget="tags", group="Launchers", order=order, placeholder=placeholder, help=help_text),
    )


class SettingsConfig(BaseModel):
    editor: LauncherTemplate = _launcher_field(
        "zed {path}", "Command that opens a project in your editor; {path} is the folder or file to open.", 1
    )
    terminal: LauncherTemplate = _launcher_field(
        "wt -d {path}", "Command that opens a terminal; it also starts in the folder, so {path} may be omitted.", 2
    )
    file_manager: LauncherTemplate = _launcher_field(
        "explorer {path}", "Command that shows a folder in your file manager.", 3
    )


class DetectedTool(BaseModel):
    kind: ToolKind = Field(json_schema_extra=ui(format="badge"))
    id: str = Field(json_schema_extra=ui(format="code"))
    label: str
    found: bool
    executable: str | None = Field(None, json_schema_extra=ui(format="path"))
    command: list[str] = Field(json_schema_extra=ui(widget="tags"))


class DetectedTools(BaseModel):
    tools: list[DetectedTool] = Field(json_schema_extra=ui(columns=["kind", "label", "found", "executable"]))


@dataclass(frozen=True)
class ToolCandidate:
    kind: ToolKind
    id: str
    label: str
    command: tuple[str, ...]
    bundled: bool = False  # ships with the OS, so it counts as found without a PATH probe


EDITOR_CANDIDATES = tuple(
    ToolCandidate("editor", name, label, (name, f"{{{PATH_PLACEHOLDER}}}"))
    for name, label in (
        ("zed", "Zed"),
        ("code", "VS Code"),
        ("idea", "IntelliJ IDEA"),
        ("pycharm", "PyCharm"),
        ("webstorm", "WebStorm"),
        ("rustrover", "RustRover"),
        ("clion", "CLion"),
    )
)


WINDOWS_CANDIDATES: tuple[ToolCandidate, ...] = (
    ToolCandidate("terminal", "windows-terminal", "Windows Terminal", ("wt", "-d", "{path}")),
    ToolCandidate(
        "terminal", "cmd", "Command Prompt", ("cmd", "/c", "start", "cmd", "/K", "cd", "/d", "{path}"), bundled=True
    ),
    ToolCandidate("file-manager", "explorer", "File Explorer", ("explorer", "{path}"), bundled=True),
)
MACOS_CANDIDATES: tuple[ToolCandidate, ...] = (
    ToolCandidate("terminal", "terminal-app", "Terminal", ("open", "-a", "Terminal", "{path}"), bundled=True),
    ToolCandidate("file-manager", "finder", "Finder", ("open", "{path}"), bundled=True),
)
LINUX_CANDIDATES: tuple[ToolCandidate, ...] = (
    ToolCandidate("terminal", "x-terminal-emulator", "System terminal", ("x-terminal-emulator",)),
    ToolCandidate("terminal", "gnome-terminal", "GNOME Terminal", ("gnome-terminal", "--working-directory={path}")),
    ToolCandidate("terminal", "konsole", "Konsole", ("konsole", "--workdir", "{path}")),
    ToolCandidate("file-manager", "xdg-open", "Default file manager", ("xdg-open", "{path}"), bundled=True),
)


def _candidates(platform: str) -> tuple[ToolCandidate, ...]:
    platform_tools = {"win32": WINDOWS_CANDIDATES, "darwin": MACOS_CANDIDATES}.get(platform, LINUX_CANDIDATES)
    return (*EDITOR_CANDIDATES, *platform_tools)


def probe_tools(platform: str = sys.platform) -> list[DetectedTool]:
    """Probe PATH for every known editor and terminal; candidates are listed in preference order."""
    tools: list[DetectedTool] = []
    for candidate in _candidates(platform):
        executable = shutil.which(candidate.command[0])
        tools.append(
            DetectedTool(
                kind=candidate.kind,
                id=candidate.id,
                label=candidate.label,
                found=candidate.bundled or executable is not None,
                executable=executable,
                command=list(candidate.command),
            )
        )
    return tools


def default_config(tools: Sequence[DetectedTool]) -> SettingsConfig:
    """The most preferred found tool of each kind; empty where nothing was found."""

    def preferred(kind: ToolKind) -> list[str]:
        return next((tool.command for tool in tools if tool.kind == kind and tool.found), [])

    return SettingsConfig(
        editor=preferred("editor"), terminal=preferred("terminal"), file_manager=preferred("file-manager")
    )


# --- models ---


class BackupInfo(BaseModel):
    name: str = Field(json_schema_extra=ui(format="code"))
    path: str = Field(json_schema_extra=ui(format="path"))
    size_bytes: int = Field(json_schema_extra=ui(format="bytes", short_label="size"))
    created_at: float = Field(json_schema_extra=ui(format="relative-time", short_label="created"))


class BackupList(BaseModel):
    backups: list[BackupInfo] = Field(json_schema_extra=ui(columns=["name", "created_at", "size_bytes"]))


class SettingsState(BaseModel):
    backups: list[BackupInfo] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["name", "created_at", "size_bytes"], help="Newest first."),
    )


class OpenInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    path: str | None = Field(
        None,
        json_schema_extra=ui(
            widget="path-dir",
            help="Inside the project or the dashboard's worktrees; relative paths start at the project root. "
            "Empty opens the project root.",
        ),
    )


class Launched(BaseModel):
    argv: list[str] = Field(json_schema_extra=ui(widget="tags"))
    cwd: str = Field(json_schema_extra=ui(format="path"))
    pid: int


class RestoreInput(BaseModel):
    name: str = Field(json_schema_extra=ui(placeholder="20261105-101010-123456.sqlite3", help="A name from Backups."))


class Restored(BaseModel):
    name: str
    safety_backup: str = Field(json_schema_extra=ui(help="A backup of the state that was replaced."))
    services_reloaded: int


class ExportReportInput(BaseModel):
    format: ReportFormat = Field("markdown", json_schema_extra=ui(widget="select"))


class ReportFile(BaseModel):
    path: str = Field(json_schema_extra=ui(format="path"))
    format: ReportFormat
    size_bytes: int = Field(json_schema_extra=ui(format="bytes"))
    projects: int


class SettingsService(Service[SettingsConfig, SettingsState]):
    id = "settings"
    title = "Settings"
    description = "Editor, terminal, and file-manager launchers, backup and restore, and report export."
    Config = SettingsConfig
    State = SettingsState

    def __init__(self, runtime: Runtime, projects: ProjectsService) -> None:
        super().__init__(runtime)
        self._projects = projects

    @override
    async def start(self) -> None:
        if await self.runtime.db.kv_get("config", self.id) is None:
            await self.update_config(default_config(await asyncio.to_thread(probe_tools)))
        self.update_state(backups=self._list_backups())

    # --- opening projects ---

    def _target(self, record: ProjectRecord, path: str | None) -> Path:
        """The existing path to open: the project root by default, else `path` if it stays inside a permitted root."""
        root = record.path.resolve()
        if path is None:
            return root
        target = (root / path).resolve()
        permitted = (root, self.runtime.paths.worktrees.resolve())
        if not any(target.is_relative_to(base) for base in permitted):
            raise ActionError(
                f"{path!r} is outside the project ({root}) and the dashboard's worktrees ({permitted[1]})", status=422
            )
        if not target.exists():
            raise ActionError(f"{target} does not exist", status=404)
        return target

    def _launch(self, template: Sequence[str], target: Path, *, what: str) -> Launched:
        if not template:
            raise ActionError(f"no {what} is configured; set one in Settings", status=409)
        argv = render_launch(template, target)
        cwd = target if target.is_dir() else target.parent
        try:
            pid = tree.launch_detached(argv, cwd)
        except tree.ExecutableNotFoundError as error:
            raise ActionError(f"{error}; fix the {what} in Settings", status=409) from error
        return Launched(argv=argv, cwd=str(cwd), pid=pid)

    def _folder(self, record: ProjectRecord, path: str | None) -> Path:
        target = self._target(record, path)
        if not target.is_dir():
            raise ActionError(f"{target} is not a folder", status=422)
        return target

    @action(title="Open in editor")
    async def open_in_editor(self, request: OpenInput) -> Launched:
        """Open the project (or a file or folder inside it) in the editor; a project's own editor wins."""
        record = self._projects.record(request.project_id)
        template = self._projects.settings(record.id).editor_command or self.config.editor
        return self._launch(template, self._target(record, request.path), what="editor")

    @action(title="Open in terminal")
    async def open_in_terminal(self, request: OpenInput) -> Launched:
        """Open a terminal in the project folder (or a folder inside it)."""
        record = self._projects.record(request.project_id)
        return self._launch(self.config.terminal, self._folder(record, request.path), what="terminal")

    @action(title="Open folder")
    async def open_folder(self, request: OpenInput) -> Launched:
        """Show the project folder (or a folder inside it) in the file manager."""
        record = self._projects.record(request.project_id)
        return self._launch(self.config.file_manager, self._folder(record, request.path), what="file manager")

    @action(title="Detect tools", query=True)
    async def detect_tools(self, _request: NoInput) -> DetectedTools:
        """Which editors and terminals are installed; the first found of each kind is the default."""
        return DetectedTools(tools=await asyncio.to_thread(probe_tools))

    # --- backup and restore ---

    def _describe(self, path: Path) -> BackupInfo:
        stat = path.stat()
        return BackupInfo(name=path.name, path=str(path), size_bytes=stat.st_size, created_at=stat.st_mtime)

    def _list_backups(self) -> list[BackupInfo]:
        directory = self.runtime.paths.backups
        found = [self._describe(path) for path in directory.glob(f"*{BACKUP_SUFFIX}") if path.is_file()]
        return sorted(found, key=lambda info: (info.created_at, info.name), reverse=True)

    async def _write_backup(self, prefix: str = "") -> BackupInfo:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S-%f")
        destination = self.runtime.paths.backups / f"{prefix}{stamp}{BACKUP_SUFFIX}"
        await self.runtime.db.backup_to(destination)
        info = self._describe(destination)
        self.update_state(backups=self._list_backups())
        return info

    def _backup_file(self, name: str) -> Path:
        """The named backup, which must be a snapshot file directly inside the backups folder."""
        directory = self.runtime.paths.backups.resolve()
        candidate = (directory / name).resolve()
        if candidate.parent != directory or candidate.suffix != BACKUP_SUFFIX:
            raise ActionError(f"{name!r} is not a backup in {directory}", status=422)
        if not candidate.is_file():
            raise ActionError(f"no backup named {name!r}", status=404)
        return candidate

    @action(title="Back up dashboard state")
    async def backup(self, _request: NoInput) -> BackupInfo:
        """Write a consistent snapshot of the dashboard database to the backups folder."""
        return await self._write_backup()

    @action(title="Backups", query=True)
    async def backups(self, _request: NoInput) -> BackupList:
        """Every backup in the backups folder, newest first."""
        return BackupList(backups=self._list_backups())

    @action(title="Restore backup", danger=True, confirm=True)
    async def restore(self, request: RestoreInput) -> Restored:
        """Replace all dashboard state with a backup and reload every service; the replaced state is backed up first."""
        source = self._backup_file(request.name)
        safety = await self._write_backup(PRE_RESTORE_PREFIX)
        services = list(self.runtime.services.values())
        for service in reversed(services):
            try:
                await service.stop()
            except Exception:  # one service failing to stop must not leave the others running
                self.runtime.log.exception("service %s failed to stop for the restore", service.id)
        await self.runtime.db.restore_from(source)
        for service in services:
            # A config absent from the snapshot must fall back to defaults, not keep the replaced one.
            service.config = service.Config()
            await service.load_config()
        for service in services:
            await service.start()
        return Restored(name=source.name, safety_backup=safety.name, services_reloaded=len(services))

    # --- report export ---

    @action(title="Export report")
    async def export_report(self, request: ExportReportInput) -> ReportFile:
        """Write the overview as a Markdown or JSON report to the reports folder."""
        projects = self._projects.state.projects
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S-%f")
        destination = self.runtime.paths.reports / f"forge-dashboard-report-{stamp}.{REPORT_EXTENSIONS[request.format]}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if request.format == "json":
            destination.write_bytes(TypeAdapter(list[ProjectSummary]).dump_json(projects, indent=2))
        else:
            destination.write_text(render_markdown(projects), encoding="utf-8")
        return ReportFile(
            path=str(destination), format=request.format, size_bytes=destination.stat().st_size, projects=len(projects)
        )
