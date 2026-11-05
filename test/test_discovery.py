"""Discovery: classification, scanning, caching, folder browsing, and the watcher."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from testing_support import wait_for

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.domain import discovery as rules
from forge_dashboard.services.discovery import (
    BrowseInput,
    ChangeKind,
    DiscoveryService,
    PathInput,
    _scan,
    classify_change,
)
from forge_dashboard.services.jobs import JobsService


def test_forge_repo_is_recognised_by_its_signature(forge_repo: Path) -> None:
    assert rules.is_forge_repo(forge_repo)
    assert rules.classify(forge_repo, None) == "standards-source"


def test_manifest_makes_a_managed_project(make_project: Callable[..., Path]) -> None:
    assert rules.classify(make_project("Alpha"), None) == "managed"


def test_forge_like_needs_forge_headings_and_hook_ids(forge_repo: Path, tmp_path: Path) -> None:
    signature = rules.ForgeSignature.read(forge_repo)
    assert "Verifying" in signature.headings and "ruff-check" in signature.hook_ids
    lookalike = tmp_path / "lookalike"
    lookalike.mkdir()
    (lookalike / "AGENTS.md").write_text((forge_repo / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8")
    assert rules.classify(lookalike, signature) is None
    (lookalike / ".pre-commit-config.yaml").write_text(
        (forge_repo / ".pre-commit-config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert rules.classify(lookalike, signature) == "unmanifested"
    (lookalike / "AGENTS.md").write_text("# other\n\n## Setup\n\n## Verifying\n", encoding="utf-8")
    assert rules.classify(lookalike, signature) is None


def test_hook_ids_tolerate_missing_and_broken_configs(tmp_path: Path) -> None:
    assert rules.hook_ids(tmp_path) == []
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: [", encoding="utf-8")
    assert rules.hook_ids(tmp_path) == []


def test_scan_prunes_dependency_trees_hidden_dirs_and_depth(tmp_path: Path) -> None:
    for relative in ("a/forge.project.toml", "node_modules/x/forge.project.toml", ".hidden/forge.project.toml",
                     "b/c/d/e/forge.project.toml", "skip/forge.project.toml"):  # fmt: skip
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    seen: list[int] = []
    found = _scan([tmp_path], 3, frozenset({tmp_path / "skip"}), lambda visited, _current: seen.append(visited))
    assert found == [tmp_path / "a"]
    assert seen


@pytest.mark.parametrize(
    ("relative", "kind"),
    [
        ("forge.project.toml", "manifest"),
        (".git/HEAD", "git"),
        (".git/refs/heads/main", "git"),
        (".git/logs/HEAD", "git"),
        (".git/objects/ab/cdef", None),
        ("src/app.py", "worktree"),
        ("web/node_modules/x/index.js", None),
        ("src/__pycache__/app.cpython-312.pyc", None),
    ],
)
def test_changes_are_classified_by_path(tmp_path: Path, relative: str, kind: ChangeKind | None) -> None:
    assert classify_change(tmp_path, tmp_path / relative) == kind


def test_changes_outside_the_project_are_ignored(tmp_path: Path) -> None:
    assert classify_change(tmp_path / "a", tmp_path / "b" / "x.py") is None


@pytest.fixture
async def discovery_runtime(data_paths: DataPaths, tmp_path: Path) -> AsyncIterator[tuple[Runtime, DiscoveryService]]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    runtime.register([jobs, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path)], "max_depth": 4})
    await runtime.start()
    try:
        yield runtime, discovery
    finally:
        await runtime.stop()


async def test_first_launch_scans_and_detects_the_forge_repo(
    forge_repo: Path, make_project: Callable[..., Path], tmp_path: Path, data_paths: DataPaths
) -> None:
    alpha = make_project("Alpha")
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    runtime.register([jobs, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path)], "max_depth": 4})
    await runtime.start()
    try:
        await wait_for(lambda: discovery.state.scanned_at is not None)
        kinds = {Path(c.path): c.kind for c in discovery.state.candidates}
        assert kinds == {forge_repo: "standards-source", alpha: "managed"}
        assert discovery.state.forge_repo == str(forge_repo)
        assert discovery.state.forge_repo_source == "detected"
        assert discovery.state.scan.found == 2 and not discovery.state.scan.running
    finally:
        await runtime.stop()

    relaunched_at = time.time()
    cached = Runtime(data_paths)
    cached_jobs = JobsService(cached)
    cached_discovery = DiscoveryService(cached, cached_jobs)
    cached.register([cached_jobs, cached_discovery])
    await cached.start()
    try:
        assert {Path(c.path) for c in cached_discovery.state.candidates} == {forge_repo, alpha}
        fresh_scans = [job for job in cached_jobs.state.jobs if job.created_at >= relaunched_at]
        assert fresh_scans == [], "a fresh cache must not trigger a rescan"
    finally:
        await cached.stop()


async def test_added_projects_are_listed_and_removed_ones_ignored(
    discovery_runtime: tuple[Runtime, DiscoveryService], tmp_path: Path
) -> None:
    _runtime, discovery = discovery_runtime
    loose = tmp_path / "elsewhere" / "loose"
    loose.mkdir(parents=True)
    await discovery.add_project(PathInput(path=str(loose)))
    [candidate] = [c for c in discovery.state.candidates if Path(c.path) == loose.resolve()]
    assert candidate.kind == "unmanifested" and candidate.added_by_hand
    await discovery.remove_project(PathInput(path=candidate.path))
    assert all(Path(c.path) != loose.resolve() for c in discovery.state.candidates)


async def test_browse_lists_subfolders_and_marks_projects(
    discovery_runtime: tuple[Runtime, DiscoveryService], make_project: Callable[..., Path], tmp_path: Path
) -> None:
    _runtime, discovery = discovery_runtime
    make_project("Beta")
    listing = await discovery.browse(BrowseInput(path=str(tmp_path / "projects")))
    assert [(entry.name, entry.is_project) for entry in listing.entries] == [("Beta", True)]
    assert listing.parent == str(tmp_path)
    roots = await discovery.browse(BrowseInput())
    assert roots.entries


async def test_watcher_reports_manifest_and_worktree_changes(
    discovery_runtime: tuple[Runtime, DiscoveryService], make_project: Callable[..., Path]
) -> None:
    _runtime, discovery = discovery_runtime
    gamma = make_project("Gamma")
    changes: list[tuple[Path, frozenset[ChangeKind]]] = []

    async def record(path: Path, kinds: frozenset[ChangeKind]) -> None:
        changes.append((path, kinds))

    discovery.on_change(record)
    await discovery.add_project(PathInput(path=str(gamma)))
    await asyncio.sleep(0.5)
    (gamma / "forge.project.toml").write_text("schema = 1\n", encoding="utf-8")
    (gamma / "notes.txt").write_text("hello", encoding="utf-8")
    await wait_for(lambda: any("manifest" in kinds for _path, kinds in changes), timeout_s=10)
    seen = set().union(*(kinds for path, kinds in changes if path == gamma.resolve()))
    assert {"manifest", "worktree"} <= seen
