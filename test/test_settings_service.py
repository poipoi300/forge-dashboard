"""Launchers, tool detection, backup and restore, and report export."""

import json
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, override

import pytest
from pydantic import BaseModel, ValidationError
from testing_support import wait_for

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, NoInput, Service
from forge_dashboard.process import tree
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectSettings, ProjectsService, SettingsInput
from forge_dashboard.services.settings import (
    ExportReportInput,
    OpenInput,
    RestoreInput,
    SettingsConfig,
    SettingsService,
    default_config,
    probe_tools,
)


class ProbeConfig(BaseModel):
    level: int = 1


class Empty(BaseModel):
    pass


class Probe(Service[ProbeConfig, Empty]):
    """Records the lifecycle calls the restore makes."""

    Config = ProbeConfig
    State = Empty
    title = "Probe"
    description = "Records lifecycle calls."
    calls: ClassVar[list[str]]

    @override
    async def load_config(self) -> None:
        self.calls.append(f"{self.id}:load")
        await super().load_config()

    @override
    async def start(self) -> None:
        self.calls.append(f"{self.id}:start")

    @override
    async def stop(self) -> None:
        self.calls.append(f"{self.id}:stop")


class FirstProbe(Probe):
    id = "probe-first"


class SecondProbe(Probe):
    id = "probe-second"


@dataclass
class Env:
    runtime: Runtime
    settings: SettingsService
    projects: ProjectsService
    discovery: DiscoveryService
    first: FirstProbe
    second: SecondProbe
    calls: list[str]

    async def add_project(self, root: Path) -> None:
        await self.discovery.add_project(PathInput(path=str(root)))
        await wait_for(lambda: any(record.path == root for record in self.projects.records()))
        await wait_for(lambda: len(self.projects.state.projects) == len(self.projects.records()))


@asynccontextmanager
async def running(data_paths: DataPaths, calls: list[str]) -> AsyncIterator[Env]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    settings = SettingsService(runtime, projects)
    first, second = FirstProbe(runtime), SecondProbe(runtime)
    Probe.calls = calls
    runtime.register([settings, first, second, jobs, manifests, projects, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(data_paths.root.parent / "no-projects-here")]})
    await runtime.start()
    await wait_for(lambda: projects.state.loaded)  # the startup scan must finish before a test backs up the database
    try:
        yield Env(runtime, settings, projects, discovery, first, second, calls)
    finally:
        await runtime.stop()


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The executables `shutil.which` finds; a test adds to it before the services start."""
    found = {"zed": "C:/tools/zed.exe"}
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: found.get(name))
    return found


@pytest.fixture
async def env(data_paths: DataPaths, installed: dict[str, str]) -> AsyncIterator[Env]:
    async with running(data_paths, []) as environment:
        yield environment


Launches = list[tuple[list[str], Path]]


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> Launches:
    """Replaces the detached launcher so no test ever opens a real window."""
    recorded: Launches = []

    def fake_launch(argv: list[str], cwd: Path) -> int:
        recorded.append((list(argv), cwd))
        return 4242

    monkeypatch.setattr(tree, "launch_detached", fake_launch)
    return recorded


@pytest.fixture
async def alpha(env: Env, make_project: Callable[..., Path]) -> Path:
    root = make_project("Alpha").resolve()
    await env.add_project(root)
    return root


# --- detection ---


@pytest.mark.parametrize(
    ("found", "editor"),
    [
        (["zed", "code", "idea"], ["zed", "{path}"]),
        (["code", "idea", "pycharm"], ["code", "{path}"]),
        (["clion", "rustrover", "webstorm"], ["webstorm", "{path}"]),
        (["clion", "pycharm", "idea"], ["idea", "{path}"]),
        (["clion"], ["clion", "{path}"]),
        ([], []),
    ],
)
def test_the_default_editor_is_the_first_found_of_zed_vs_code_then_jetbrains(
    installed: dict[str, str], found: list[str], editor: list[str]
) -> None:
    installed.clear()
    installed.update({name: f"/usr/bin/{name}" for name in found})
    assert default_config(probe_tools("linux")).editor == editor


@pytest.mark.parametrize(
    ("platform", "found", "terminal", "file_manager"),
    [
        ("win32", ["wt"], ["wt", "-d", "{path}"], ["explorer", "{path}"]),
        ("win32", [], ["cmd", "/c", "start", "cmd", "/K", "cd", "/d", "{path}"], ["explorer", "{path}"]),
        ("darwin", [], ["open", "-a", "Terminal", "{path}"], ["open", "{path}"]),
        (
            "linux",
            ["konsole", "gnome-terminal", "x-terminal-emulator"],
            ["x-terminal-emulator"],
            ["xdg-open", "{path}"],
        ),
        (
            "linux",
            ["konsole", "gnome-terminal"],
            ["gnome-terminal", "--working-directory={path}"],
            ["xdg-open", "{path}"],
        ),
        ("linux", ["konsole"], ["konsole", "--workdir", "{path}"], ["xdg-open", "{path}"]),
        ("linux", [], [], ["xdg-open", "{path}"]),
    ],
)
def test_terminal_and_file_manager_defaults_follow_the_platform(
    installed: dict[str, str], platform: str, found: list[str], terminal: list[str], file_manager: list[str]
) -> None:
    installed.clear()
    installed.update({name: f"/usr/bin/{name}" for name in found})
    config = default_config(probe_tools(platform))
    assert (config.terminal, config.file_manager) == (terminal, file_manager)


async def test_detect_tools_reports_what_was_found(env: Env, installed: dict[str, str]) -> None:
    installed["code"] = "C:/tools/code.cmd"
    detected = await env.settings.detect_tools(NoInput())

    editors = {tool.id: tool for tool in detected.tools if tool.kind == "editor"}
    assert (editors["zed"].found, editors["zed"].executable) == (True, "C:/tools/zed.exe")
    assert (editors["code"].found, editors["code"].executable) == (True, "C:/tools/code.cmd")
    assert (editors["clion"].found, editors["clion"].executable) == (False, None)
    assert {tool.kind for tool in detected.tools} == {"editor", "terminal", "file-manager"}


async def test_first_start_stores_the_detected_launchers_and_later_starts_keep_the_users_choice(
    data_paths: DataPaths, installed: dict[str, str]
) -> None:
    async with running(data_paths, []) as first:
        assert first.settings.config.editor == ["zed", "{path}"]
        assert await first.runtime.db.kv_get("config", "settings") is not None
        await first.settings.update_config(SettingsConfig(editor=[], terminal=["my-term"], file_manager=[]))

    installed["code"] = "C:/tools/code.cmd"
    async with running(data_paths, []) as second:
        assert second.settings.config == SettingsConfig(editor=[], terminal=["my-term"], file_manager=[])


def test_launcher_templates_reject_unknown_placeholders_and_blank_executables() -> None:
    with pytest.raises(ValidationError, match=r"allowed here: \{path\}"):
        SettingsConfig(editor=["zed", "{folder}"])
    with pytest.raises(ValidationError, match="must name an executable"):
        SettingsConfig(terminal=["", "{path}"])
    assert SettingsConfig(editor=["zed", "{{literal}}", "{path}"]).editor[1] == "{{literal}}"


# --- opening ---


async def test_open_in_editor_launches_the_template_with_the_project_root(
    env: Env, alpha: Path, launches: Launches
) -> None:
    launched = await env.settings.open_in_editor(OpenInput(project_id="alpha"))

    assert launches == [(["zed", str(alpha)], alpha)]
    assert (launched.argv, launched.cwd, launched.pid) == (["zed", str(alpha)], str(alpha), 4242)


async def test_open_in_editor_opens_a_file_from_its_folder(env: Env, alpha: Path, launches: Launches) -> None:
    await env.settings.open_in_editor(OpenInput(project_id="alpha", path="README.md"))
    await env.settings.open_in_editor(OpenInput(project_id="alpha", path=str(alpha / "README.md")))

    expected = (["zed", str(alpha / "README.md")], alpha)
    assert launches == [expected, expected]


async def test_a_parked_fork_worktree_can_be_opened(env: Env, alpha: Path, launches: Launches) -> None:
    worktree = env.runtime.paths.worktrees.resolve() / "alpha" / "forge-sync-main-20261105"
    worktree.mkdir(parents=True)
    await env.settings.open_in_editor(OpenInput(project_id="alpha", path=str(worktree)))

    assert launches == [(["zed", str(worktree)], worktree)]


async def test_a_project_editor_override_beats_the_global_editor(env: Env, alpha: Path, launches: Launches) -> None:
    override = ProjectSettings(editor_command=["code", "--goto", "{path}"])
    await env.projects.save_settings(SettingsInput(project_id="alpha", settings=override))
    await env.settings.open_in_editor(OpenInput(project_id="alpha"))

    assert launches == [(["code", "--goto", str(alpha)], alpha)]


async def test_a_malformed_project_editor_override_is_refused_before_launching(
    env: Env, alpha: Path, launches: Launches
) -> None:
    override = ProjectSettings(editor_command=["code", "{folder}"])
    await env.projects.save_settings(SettingsInput(project_id="alpha", settings=override))
    with pytest.raises(ActionError, match=r"allowed here: \{path\}") as failure:
        await env.settings.open_in_editor(OpenInput(project_id="alpha"))
    assert failure.value.status == 422 and launches == []


async def test_open_in_editor_without_a_configured_editor_is_a_conflict(
    env: Env, alpha: Path, launches: Launches
) -> None:
    await env.settings.update_config(SettingsConfig(editor=[], terminal=["t"], file_manager=["f"]))
    with pytest.raises(ActionError, match="no editor is configured") as failure:
        await env.settings.open_in_editor(OpenInput(project_id="alpha"))
    assert failure.value.status == 409 and launches == []


async def test_a_launcher_that_is_not_on_path_is_a_conflict_naming_the_executable(
    env: Env, alpha: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(argv: list[str], cwd: Path) -> int:
        raise tree.ExecutableNotFoundError(argv[0])

    monkeypatch.setattr(tree, "launch_detached", missing)
    with pytest.raises(ActionError, match="'zed' is not on PATH; fix the editor in Settings") as failure:
        await env.settings.open_in_editor(OpenInput(project_id="alpha"))
    assert failure.value.status == 409


async def test_unknown_projects_and_missing_paths_are_not_found(env: Env, alpha: Path, launches: Launches) -> None:
    with pytest.raises(ActionError) as unknown:
        await env.settings.open_in_editor(OpenInput(project_id="nope"))
    with pytest.raises(ActionError) as missing:
        await env.settings.open_in_editor(OpenInput(project_id="alpha", path="src/missing.py"))
    assert (unknown.value.status, missing.value.status) == (404, 404) and launches == []


async def test_paths_outside_the_project_and_the_worktrees_are_rejected(
    env: Env, alpha: Path, launches: Launches
) -> None:
    sibling = alpha.parent / "Alpha-other"
    sibling.mkdir()
    outside = (alpha.parent.parent / "elsewhere").resolve()
    outside.mkdir()
    for escape in ("..", "../Alpha-other", str(sibling), str(outside), "src/../..", str(env.runtime.paths.root)):
        with pytest.raises(ActionError, match="is outside the project") as failure:
            await env.settings.open_in_editor(OpenInput(project_id="alpha", path=escape))
        assert failure.value.status == 422, escape
    assert launches == []


async def test_a_symlink_inside_the_project_cannot_reach_outside_it(env: Env, alpha: Path, launches: Launches) -> None:
    outside = alpha.parent / "outside-target"
    outside.mkdir()
    link = alpha / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs privileges on this machine")
    with pytest.raises(ActionError, match="is outside the project"):
        await env.settings.open_in_editor(OpenInput(project_id="alpha", path="link"))
    assert launches == []


async def test_open_in_terminal_and_folder_start_in_the_folder(env: Env, alpha: Path, launches: Launches) -> None:
    (alpha / "src").mkdir()
    await env.settings.update_config(
        SettingsConfig(editor=["zed", "{path}"], terminal=["term", "--cwd={path}"], file_manager=["files", "{path}"])
    )
    await env.settings.open_in_terminal(OpenInput(project_id="alpha"))
    await env.settings.open_folder(OpenInput(project_id="alpha", path="src"))

    assert launches == [
        (["term", f"--cwd={alpha}"], alpha),
        (["files", str(alpha / "src")], alpha / "src"),
    ]


async def test_terminal_and_folder_refuse_files_and_missing_launchers(
    env: Env, alpha: Path, launches: Launches
) -> None:
    for open_it in (env.settings.open_in_terminal, env.settings.open_folder):
        with pytest.raises(ActionError, match="is not a folder") as not_a_folder:
            await open_it(OpenInput(project_id="alpha", path="README.md"))
        assert not_a_folder.value.status == 422
    await env.settings.update_config(SettingsConfig(editor=["zed", "{path}"], terminal=[], file_manager=[]))
    with pytest.raises(ActionError, match="no terminal is configured"):
        await env.settings.open_in_terminal(OpenInput(project_id="alpha"))
    with pytest.raises(ActionError, match="no file manager is configured"):
        await env.settings.open_folder(OpenInput(project_id="alpha"))
    assert launches == []


# --- backup and restore ---


async def test_backup_writes_a_snapshot_and_lists_it_newest_first(env: Env) -> None:
    first = await env.settings.backup(NoInput())
    second = await env.settings.backup(NoInput())

    assert Path(first.path).parent == env.runtime.paths.backups and Path(first.path).is_file()
    assert first.size_bytes == Path(first.path).stat().st_size > 0 and first.name.endswith(".sqlite3")
    listed = (await env.settings.backups(NoInput())).backups
    assert [info.name for info in listed] == [second.name, first.name]
    assert [info.name for info in env.settings.state.backups] == [second.name, first.name]


async def test_restore_brings_back_the_data_and_configs_of_the_backup(env: Env) -> None:
    await env.runtime.db.kv_put("probe-data", "kept", "before")
    await env.first.update_config(ProbeConfig(level=5))
    await env.settings.update_config(SettingsConfig(editor=["my-editor", "{path}"], terminal=[], file_manager=[]))
    snapshot = await env.settings.backup(NoInput())

    await env.runtime.db.kv_put("probe-data", "kept", "after")
    await env.runtime.db.kv_put("probe-data", "added-later", "x")
    await env.first.update_config(ProbeConfig(level=9))
    await env.settings.update_config(SettingsConfig(editor=["other", "{path}"], terminal=[], file_manager=[]))
    await env.second.update_config(ProbeConfig(level=3))

    restored = await env.settings.restore(RestoreInput(name=snapshot.name))

    assert await env.runtime.db.kv_all("probe-data") == {"kept": "before"}
    assert env.first.config.level == 5
    assert env.settings.config.editor == ["my-editor", "{path}"]
    assert env.second.config.level == 1, "a config the snapshot lacks falls back to its defaults"
    assert (restored.name, restored.services_reloaded) == (snapshot.name, len(env.runtime.services))


async def test_restore_stops_services_in_reverse_then_loads_configs_then_starts_in_order(env: Env) -> None:
    snapshot = await env.settings.backup(NoInput())
    env.calls.clear()

    await env.settings.restore(RestoreInput(name=snapshot.name))

    assert env.calls == [
        "probe-second:stop",
        "probe-first:stop",
        "probe-first:load",
        "probe-second:load",
        "probe-first:start",
        "probe-second:start",
    ]


async def test_restore_first_backs_up_the_state_it_replaces(env: Env) -> None:
    snapshot = await env.settings.backup(NoInput())
    await env.runtime.db.kv_put("probe-data", "precious", "unsaved work")

    restored = await env.settings.restore(RestoreInput(name=snapshot.name))
    assert restored.safety_backup.startswith("pre-restore-")
    assert restored.safety_backup in [info.name for info in env.settings.state.backups]
    assert await env.runtime.db.kv_get("probe-data", "precious") is None

    await env.settings.restore(RestoreInput(name=restored.safety_backup))
    assert await env.runtime.db.kv_get("probe-data", "precious") == "unsaved work"


async def test_restore_keeps_working_when_a_service_fails_to_stop(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    snapshot = await env.settings.backup(NoInput())

    async def broken_stop() -> None:
        raise RuntimeError("stuck")

    env.second.stop = broken_stop  # type: ignore[method-assign]
    await env.settings.restore(RestoreInput(name=snapshot.name))

    assert "service probe-second failed to stop for the restore" in caplog.text
    assert "probe-first:start" in env.calls


@pytest.mark.parametrize(
    "name", ["../escape.sqlite3", "sub/../../escape.sqlite3", "C:/Windows/escape.sqlite3", "notes.txt", "", "."]
)
async def test_restore_rejects_names_that_are_not_backups_in_the_backups_folder(env: Env, name: str) -> None:
    (env.runtime.paths.root / "escape.sqlite3").write_bytes(b"not a database")
    (env.runtime.paths.backups / "notes.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(ActionError, match="is not a backup in") as failure:
        await env.settings.restore(RestoreInput(name=name))
    assert failure.value.status == 422


async def test_restore_of_a_missing_backup_is_not_found(env: Env) -> None:
    with pytest.raises(ActionError, match="no backup named") as failure:
        await env.settings.restore(RestoreInput(name="gone.sqlite3"))
    assert failure.value.status == 404


# --- report export ---


async def test_export_report_writes_markdown_and_json_to_the_reports_folder(env: Env, alpha: Path) -> None:
    markdown = await env.settings.export_report(ExportReportInput())
    as_json = await env.settings.export_report(ExportReportInput(format="json"))

    markdown_path, json_path = Path(markdown.path), Path(as_json.path)
    assert markdown_path.parent == json_path.parent == env.runtime.paths.reports
    assert markdown_path.suffix == ".md" and json_path.suffix == ".json"
    assert "| Alpha |" in markdown_path.read_text(encoding="utf-8")
    documents: list[dict[str, Any]] = json.loads(json_path.read_text(encoding="utf-8"))
    assert [document["id"] for document in documents] == ["alpha"]
    assert (markdown.projects, as_json.projects) == (1, 1)
    assert markdown.size_bytes == markdown_path.stat().st_size and as_json.size_bytes == json_path.stat().st_size
