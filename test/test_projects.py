"""The project registry: ids, manifests, listeners, user state, and the overview state."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from testing_support import wait_for

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, NoInput, ProjectRef
from forge_dashboard.domain.summary import CheckFacts, HealthLevel, IssueFacts, Lifecycle
from forge_dashboard.services.discovery import ChangeKind, DiscoveryService, PathInput
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.manifests import ManifestsService, ValidateTextInput
from forge_dashboard.services.projects import (
    FavoriteInput,
    NotesInput,
    ProjectListener,
    ProjectRecord,
    ProjectSettings,
    ProjectsService,
    SettingsInput,
    StatusInput,
    assign_ids,
    raw_manifest_id,
    slugify,
)


def test_ids_are_stable_and_duplicates_get_a_path_suffix(tmp_path: Path) -> None:
    ids = assign_ids([(tmp_path / "b", "app"), (tmp_path / "a", "app"), (tmp_path / "c", "tool")])
    assert ids[tmp_path / "a"] == "app"
    assert ids[tmp_path / "b"].startswith("app-") and len(ids[tmp_path / "b"]) == len("app-") + 6
    assert ids == assign_ids([(tmp_path / "c", "tool"), (tmp_path / "a", "app"), (tmp_path / "b", "app")])


def test_slugify_and_raw_ids(tmp_path: Path) -> None:
    assert slugify("My Cool_Project!") == "my-cool-project"
    assert slugify("***") == "project"
    (tmp_path / "forge.project.toml").write_text('[project]\nid = "kept-id"\nname = 1\n', encoding="utf-8")
    assert raw_manifest_id(tmp_path) == "kept-id"
    (tmp_path / "forge.project.toml").write_text('[project]\nid = "Not A Slug"\n', encoding="utf-8")
    assert raw_manifest_id(tmp_path) is None


class Recorder(ProjectListener):
    def __init__(self) -> None:
        self.upserted: list[str] = []
        self.removed: list[str] = []
        self.changes: list[tuple[str, frozenset[ChangeKind]]] = []

    async def project_upserted(self, record: ProjectRecord) -> None:
        self.upserted.append(record.id)

    async def project_removed(self, project_id: str) -> None:
        self.removed.append(project_id)

    async def files_changed(self, record: ProjectRecord, kinds: frozenset[ChangeKind]) -> None:
        self.changes.append((record.id, kinds))


@pytest.fixture
async def registry(
    data_paths: DataPaths, tmp_path: Path, forge_repo: Path
) -> AsyncIterator[tuple[ProjectsService, DiscoveryService, Recorder]]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    recorder = Recorder()
    projects.listen(recorder)
    runtime.register([jobs, manifests, projects, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path)], "max_depth": 4})
    await runtime.start()
    try:
        await wait_for(lambda: projects.state.loaded)
        yield projects, discovery, recorder
    finally:
        await runtime.stop()


async def _add(discovery: DiscoveryService, path: Path) -> None:
    await discovery.add_project(PathInput(path=str(path)))


async def test_managed_projects_get_manifest_ids_and_v1_progress(
    registry: tuple[ProjectsService, DiscoveryService, Recorder], make_project: Callable[..., Path]
) -> None:
    projects, discovery, recorder = registry
    await _add(discovery, make_project("Alpha"))
    summary = projects.summary("alpha")
    assert (summary.name, summary.role, summary.summary) == ("Alpha", "managed", "A fixture project")
    assert summary.v1 is not None and summary.v1.percent == pytest.approx(66.7)
    assert summary.v1.next_milestone == "Second milestone"
    assert "alpha" in recorder.upserted
    assert {p.id for p in projects.state.projects} >= {"alpha", "forge"}
    forge = projects.summary("forge")
    assert forge.role == "standards-source" and forge.v1 is None


async def test_invalid_manifests_show_their_errors_and_never_hide_the_project(
    registry: tuple[ProjectsService, DiscoveryService, Recorder], make_project: Callable[..., Path]
) -> None:
    projects, discovery, _recorder = registry
    broken = make_project("Broken", manifest='schema = 1\n\n[project]\nid = "broken"\nname = "Broken"\n')
    await _add(discovery, broken)
    summary = projects.summary("broken")
    assert summary.health.level is HealthLevel.FAILING
    assert summary.manifest is not None and not summary.manifest.valid and summary.manifest.error_count >= 2
    detail = await projects.detail(ProjectRef(project_id="broken"))
    assert detail.manifest is None and detail.has_manifest_file
    assert any(issue.location == "project.summary" and issue.line == 3 for issue in detail.manifest_issues)


async def test_user_state_persists_and_drives_sorting(
    registry: tuple[ProjectsService, DiscoveryService, Recorder], make_project: Callable[..., Path]
) -> None:
    projects, discovery, _recorder = registry
    await _add(discovery, make_project("Alpha"))
    await _add(discovery, make_project("Zulu"))
    await projects.set_status(StatusInput(project_id="zulu", status=Lifecycle.BLOCKED, note="needs keys"))
    await projects.set_favorite(FavoriteInput(project_id="zulu", favorite=True))
    assert projects.state.projects[0].id == "zulu"
    zulu = projects.summary("zulu")
    assert (zulu.status, zulu.status_note) == (Lifecycle.BLOCKED, "needs keys")
    assert projects.state.totals.attention >= 1
    saved = await projects.save_notes(NotesInput(project_id="zulu", markdown="# Plan"))
    assert (await projects.notes(ProjectRef(project_id="zulu"))).markdown == "# Plan" and saved.updated_at
    await projects.save_settings(SettingsInput(project_id="zulu", settings=ProjectSettings(hidden=True)))
    assert "zulu" in projects.state.hidden and all(p.id != "zulu" for p in projects.state.projects)
    assert any(p.id == "zulu" for p in (await projects.list_all(NoInput())).projects)
    with pytest.raises(ActionError, match="no project"):
        await projects.set_favorite(FavoriteInput(project_id="ghost", favorite=True))


async def test_facts_recompose_the_summary_and_totals(
    registry: tuple[ProjectsService, DiscoveryService, Recorder], make_project: Callable[..., Path]
) -> None:
    projects, discovery, _recorder = registry
    await _add(discovery, make_project("Alpha"))
    projects.put("alpha", CheckFacts(passed=1, failed=1, total=2))
    assert projects.summary("alpha").health.level is HealthLevel.FAILING
    assert projects.state.totals.failing == 1
    projects.put("alpha", IssueFacts(github="ok", open_issues=4))
    assert projects.summary("alpha").issues is not None
    projects.put("ghost", CheckFacts())


async def test_manifest_edits_reload_and_reach_listeners(
    registry: tuple[ProjectsService, DiscoveryService, Recorder], make_project: Callable[..., Path]
) -> None:
    projects, discovery, recorder = registry
    alpha = make_project("Alpha")
    await _add(discovery, alpha)
    await asyncio.sleep(1.0)  # let the restarted watcher arm before editing
    manifest = alpha / "forge.project.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("A fixture project", "Renamed"), encoding="utf-8")
    await wait_for(lambda: projects.summary("alpha").summary == "Renamed", timeout_s=10)
    assert any(project_id == "alpha" and "manifest" in kinds for project_id, kinds in recorder.changes)


async def test_polling_catches_manifest_edits_the_watcher_missed(
    registry: tuple[ProjectsService, DiscoveryService, Recorder], make_project: Callable[..., Path]
) -> None:
    projects, discovery, _recorder = registry
    alpha = make_project("Alpha")
    await _add(discovery, alpha)
    assert await projects.poll_once() == []
    manifest = alpha / "forge.project.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("A fixture project", "Polled"), encoding="utf-8")
    os.utime(manifest, (manifest.stat().st_atime, manifest.stat().st_mtime + 5))
    assert "alpha" in await projects.poll_once()
    assert projects.summary("alpha").summary == "Polled"


async def test_removed_projects_notify_listeners(
    registry: tuple[ProjectsService, DiscoveryService, Recorder], make_project: Callable[..., Path]
) -> None:
    projects, discovery, recorder = registry
    alpha = make_project("Alpha")
    await _add(discovery, alpha)
    await discovery.remove_project(PathInput(path=str(alpha.resolve())))
    assert recorder.removed == ["alpha"]
    with pytest.raises(ActionError):
        projects.record("alpha")


async def test_manifest_text_validation_reports_lines(data_paths: DataPaths) -> None:
    runtime = Runtime(data_paths)
    manifests = ManifestsService(runtime)
    result = await manifests.validate_text(ValidateTextInput(text="schema = 1\n[project]\nname = 'x'\n"))
    assert not result.valid and any(issue.line == 2 for issue in result.issues)
    schema = await manifests.json_schema(NoInput())
    assert schema.model_dump(by_alias=True)["schema"]["title"] == "forge.project.toml"
