"""Find projects on this machine and watch them for changes.

The first launch scans the configured roots (the home directory by default)
to depth 6 in the background, pruning dependency trees, caches, OS data, and
hidden directories; results are cached so later launches list projects
instantly while a periodic rescan refreshes them. A filesystem watcher on
every project triggers incremental refreshes; projects also poll as a
fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, override

import watchfiles
from pydantic import BaseModel, Field

from forge_dashboard.core.events import Event
from forge_dashboard.core.service import ActionError, Done, JobRef, NoInput, Service, action
from forge_dashboard.core.ui import ui
from forge_dashboard.domain import discovery as rules
from forge_dashboard.domain.discovery import CandidateKind, ForgeSignature
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

ChangeKind = Literal["manifest", "git", "worktree"]
ChangeListener = Callable[[Path, frozenset[ChangeKind]], Awaitable[None]]
CandidatesListener = Callable[[list["Candidate"], Path | None], Awaitable[None]]

_GIT_SIGNALS = ("HEAD", "index", "packed-refs", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD")
_IGNORED_PARTS = frozenset({"node_modules", ".venv", "venv", "target", "__pycache__", ".mypy_cache", ".ruff_cache",
                            ".pytest_cache", "dist", "coverage", ".coverage"})  # fmt: skip
WATCH_DEBOUNCE_MS = 800


class Candidate(BaseModel):
    path: str = Field(json_schema_extra=ui(format="path"))
    kind: CandidateKind = Field(
        json_schema_extra=ui(
            format="badge",
            enum_labels={"managed": "managed", "unmanifested": "no manifest", "standards-source": "standards source"},
            tones={"managed": "neutral", "unmanifested": "warn", "standards-source": "info"},
        )
    )
    found_at: float = Field(json_schema_extra=ui(format="relative-time"))
    added_by_hand: bool = Field(False, json_schema_extra=ui(hidden=True))


class ScanProgress(BaseModel):
    running: bool = False
    directories: int = Field(0, json_schema_extra=ui(help="Directories visited."))
    found: int = 0
    current: str | None = Field(None, json_schema_extra=ui(format="path"))
    started_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    finished_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    duration_s: float | None = Field(None, json_schema_extra=ui(format="duration"))


def _home() -> list[str]:
    return [str(Path.home())]


class DiscoveryConfig(BaseModel):
    roots: list[str] = Field(
        default_factory=_home,
        json_schema_extra=ui(widget="path-dir", group="Scan", order=1, help="Folders scanned for projects."),
    )
    max_depth: int = Field(6, ge=1, le=12, json_schema_extra=ui(widget="slider", min=1, max=12, group="Scan", order=2))
    rescan_hours: int = Field(
        24,
        ge=1,
        le=24 * 30,
        json_schema_extra=ui(group="Scan", order=3, unit="hours", help="Background rescan period."),
    )
    extra_projects: list[str] = Field(
        default_factory=list,
        json_schema_extra=ui(widget="path-dir", group="Projects", order=1, help="Added by path; always listed."),
    )
    ignored: list[str] = Field(
        default_factory=list, json_schema_extra=ui(widget="path-dir", group="Projects", order=2, help="Never listed.")
    )
    forge_repo: str | None = Field(
        None,
        json_schema_extra=ui(
            widget="path-dir", group="Standards source", help="Overrides the auto-detected forge repo location."
        ),
    )


class DiscoveryState(BaseModel):
    scan: ScanProgress = Field(default_factory=ScanProgress)
    scanned_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    candidates: list[Candidate] = Field(
        default_factory=list, json_schema_extra=ui(columns=["path", "kind", "found_at"])
    )
    forge_repo: str | None = Field(None, json_schema_extra=ui(format="path"))
    forge_repo_source: Literal["detected", "configured", "missing"] = "missing"
    watching: int = Field(0, json_schema_extra=ui(help="Projects under the filesystem watcher."))


class DiscoveryCache(BaseModel):
    """Scan results kept between launches."""

    scanned_at: float | None
    candidates: list[Candidate]


class PathInput(BaseModel):
    path: str = Field(json_schema_extra=ui(widget="path-dir"))


class BrowseInput(BaseModel):
    path: str | None = Field(None, json_schema_extra=ui(widget="path-dir"))


class FolderEntry(BaseModel):
    name: str
    path: str
    is_project: bool


class FolderListing(BaseModel):
    path: str | None
    parent: str | None
    entries: list[FolderEntry]


class CloneInput(BaseModel):
    url: str = Field(json_schema_extra=ui(placeholder="https://github.com/owner/repo.git"))
    root: str = Field(json_schema_extra=ui(widget="path-dir", help="Folder the clone is created in."))
    name: str | None = Field(None, json_schema_extra=ui(placeholder="defaults to the repo name"))


class ScanFinished(Event):
    type: ClassVar[str] = "discovery.scan-finished"
    found: int
    duration_s: float

    def title(self) -> str:
        return f"Scan finished: {self.found} projects in {self.duration_s:.0f}s"


def _scan(
    roots: Iterable[Path], max_depth: int, skip: frozenset[Path], progress: Callable[[int, str], None]
) -> list[Path]:
    """Breadth-first walk returning directories that look like project roots (blocking; run in a thread)."""
    relevant: list[Path] = []
    queue: list[tuple[Path, int]] = [(root, 0) for root in roots]
    visited = 0
    while queue:
        directory, depth = queue.pop(0)
        visited += 1
        if visited % 200 == 0:
            progress(visited, str(directory))
        try:
            with os.scandir(directory) as entries:
                listing = list(entries)
        except OSError:
            continue
        names = [entry.name for entry in listing]
        if rules.looks_relevant(names):
            relevant.append(directory)
        if depth >= max_depth:
            continue
        for entry in listing:
            try:
                if not entry.is_dir(follow_symlinks=False) or entry.is_junction() or entry.is_symlink():
                    continue
            except OSError:
                continue
            child = Path(entry.path)
            if rules.should_descend(entry.name) and child not in skip:
                queue.append((child, depth + 1))
    progress(visited, "")
    return relevant


def classify_change(project: Path, changed: Path) -> ChangeKind | None:
    """Which refresh a changed file calls for, or None when it is noise."""
    try:
        relative = changed.relative_to(project)
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    if parts == ("forge.project.toml",):
        return "manifest"
    if parts[0] == ".git":
        tail = parts[1:]
        if tail and (tail[0] in _GIT_SIGNALS or tail[0] == "refs" or tail[:2] == ("logs", "HEAD")):
            return "git"
        return None
    if any(part in _IGNORED_PARTS for part in parts):
        return None
    return "worktree"


def _drives() -> list[str]:
    """Filesystem roots for the folder picker, without probing each one (network drives can be slow)."""
    if sys.platform == "win32":
        return os.listdrives()
    return ["/"]


class DiscoveryService(Service[DiscoveryConfig, DiscoveryState]):
    id = "discovery"
    title = "Discovery"
    description = "Finds forge projects under the configured roots and watches them for changes."
    Config = DiscoveryConfig
    State = DiscoveryState
    events = (ScanFinished,)

    def __init__(self, runtime: Runtime, jobs: JobsService) -> None:
        super().__init__(runtime)
        self._jobs = jobs
        self._change_listeners: list[ChangeListener] = []
        self._candidate_listeners: list[CandidatesListener] = []
        self._watch_task: asyncio.Task[None] | None = None
        self._watch_stop = asyncio.Event()
        self._scan_lock = asyncio.Lock()

    # --- wiring ---

    def on_change(self, listener: ChangeListener) -> None:
        self._change_listeners.append(listener)

    def on_candidates(self, listener: CandidatesListener) -> None:
        self._candidate_listeners.append(listener)

    @property
    def forge_repo(self) -> Path | None:
        return Path(self.state.forge_repo) if self.state.forge_repo else None

    # --- lifecycle ---

    @override
    async def start(self) -> None:
        cached = await self.runtime.db.kv_get("discovery", "cache")
        if cached is not None:
            cache = DiscoveryCache.model_validate(cached)
            await self._publish(cache.candidates, cache.scanned_at)
        stale = self.state.scanned_at is None or time.time() - self.state.scanned_at > self.config.rescan_hours * 3600
        if stale:
            await self.rescan(NoInput())

    @override
    async def stop(self) -> None:
        await self._stop_watching()

    @override
    async def on_config_changed(self, previous: DiscoveryConfig) -> None:
        if (previous.roots, previous.max_depth) != (self.config.roots, self.config.max_depth):
            await self.rescan(NoInput())
            return
        await self._publish(self.state.candidates, self.state.scanned_at)

    # --- scanning ---

    async def _run_scan(self, context: JobContext | None) -> list[Candidate]:
        async with self._scan_lock:
            started = time.time()
            loop = asyncio.get_running_loop()
            self.update_state(scan=ScanProgress(running=True, started_at=started))

            def progress(visited: int, current: str) -> None:
                loop.call_soon_threadsafe(self._progress, visited, current)

            roots = [Path(root) for root in self.config.roots if Path(root).is_dir()]
            skip = frozenset({self.runtime.paths.root})
            relevant = await asyncio.to_thread(_scan, roots, self.config.max_depth, skip, progress)
            candidates = await asyncio.to_thread(self._classify_all, relevant, started)
            duration = time.time() - started
            self.update_state(
                scan=ScanProgress(
                    running=False,
                    directories=self.state.scan.directories,
                    found=len(candidates),
                    started_at=started,
                    finished_at=time.time(),
                    duration_s=duration,
                )
            )
            if context is not None:
                context.log(f"visited {self.state.scan.directories} directories, found {len(candidates)} projects")
            await self._publish(candidates, time.time())
            self.publish(ScanFinished(found=len(candidates), duration_s=duration))
            return candidates

    def _progress(self, visited: int, current: str) -> None:
        self.update_state(scan=self.state.scan.model_copy(update={"directories": visited, "current": current or None}))

    def _classify_all(self, relevant: Iterable[Path], found_at: float) -> list[Candidate]:
        relevant = list(relevant)
        forge = self._resolve_forge(relevant)
        signature = ForgeSignature.read(forge) if forge is not None else None
        previous = {candidate.path: candidate.found_at for candidate in self.state.candidates}
        candidates: list[Candidate] = []
        for directory in relevant:
            kind = rules.classify(directory, signature)
            if kind is not None:
                path = str(directory)
                candidates.append(Candidate(path=path, kind=kind, found_at=previous.get(path, found_at)))
        return candidates

    def _resolve_forge(self, relevant: Iterable[Path]) -> Path | None:
        if self.config.forge_repo:
            return Path(self.config.forge_repo)
        return next((directory for directory in relevant if rules.is_forge_repo(directory)), None)

    async def _publish(self, scanned: list[Candidate], scanned_at: float | None) -> None:
        """Merge scan results with hand-added projects, drop ignored ones, persist, and notify."""
        ignored = {str(Path(path)) for path in self.config.ignored}
        by_path = {candidate.path: candidate for candidate in scanned if not candidate.added_by_hand}
        forge = self._resolve_forge(
            Path(candidate.path) for candidate in scanned if candidate.kind == "standards-source"
        )
        signature = ForgeSignature.read(forge) if forge is not None and (forge / "AGENTS.md").is_file() else None
        for extra in self.config.extra_projects:
            directory = Path(extra)
            if not directory.is_dir():
                continue
            kind = rules.classify(directory, signature) or "unmanifested"
            by_path[str(directory)] = Candidate(
                path=str(directory), kind=kind, found_at=time.time(), added_by_hand=True
            )
        if forge is not None and str(forge) not in by_path and forge.is_dir():
            by_path[str(forge)] = Candidate(path=str(forge), kind="standards-source", found_at=time.time())
        candidates = sorted((c for path, c in by_path.items() if path not in ignored), key=lambda c: c.path.lower())
        self.update_state(
            candidates=candidates,
            scanned_at=scanned_at,
            forge_repo=str(forge) if forge is not None else None,
            forge_repo_source="configured" if self.config.forge_repo else ("detected" if forge else "missing"),
        )
        await self.runtime.db.kv_put(
            "discovery",
            "cache",
            {
                "scanned_at": scanned_at,
                "candidates": [c.model_dump(mode="json") for c in candidates if not c.added_by_hand],
            },
        )
        for listener in self._candidate_listeners:
            await listener(candidates, forge)
        await self._restart_watching([Path(c.path) for c in candidates])

    # --- watching ---

    async def _restart_watching(self, projects: list[Path]) -> None:
        await self._stop_watching()
        existing = [project for project in projects if project.is_dir()]
        self.update_state(watching=len(existing))
        if not existing:
            return
        self._watch_stop = asyncio.Event()
        self._watch_task = asyncio.create_task(self._watch(existing, self._watch_stop), name="discovery-watch")

    async def _stop_watching(self) -> None:
        if self._watch_task is None:
            return
        self._watch_stop.set()
        self._watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._watch_task
        self._watch_task = None

    async def _watch(self, projects: list[Path], stop: asyncio.Event) -> None:
        def relevant(_change: watchfiles.Change, path: str) -> bool:
            return not any(part in _IGNORED_PARTS for part in Path(path).parts)

        # Longest first, so a nested project claims its own files.
        roots = sorted(projects, key=lambda project: len(project.parts), reverse=True)
        async for changes in watchfiles.awatch(
            *projects, watch_filter=relevant, debounce=WATCH_DEBOUNCE_MS, stop_event=stop, recursive=True
        ):
            await self._dispatch(roots, {Path(path) for _change, path in changes})

    async def _dispatch(self, roots: list[Path], changed: set[Path]) -> None:
        kinds_by_project: dict[Path, set[ChangeKind]] = {}
        for path in changed:
            project = next((root for root in roots if root == path or root in path.parents), None)
            if project is None:
                continue
            kind = classify_change(project, path)
            if kind is not None:
                kinds_by_project.setdefault(project, set()).add(kind)
        for project, kinds in kinds_by_project.items():
            for listener in self._change_listeners:
                await listener(project, frozenset(kinds))

    # --- actions ---

    @action(title="Rescan", long_running=True)
    async def rescan(self, _request: NoInput) -> JobRef:
        """Scan the roots for projects again."""

        async def work(context: JobContext) -> JobOutcome:
            candidates = await self._run_scan(context)
            return JobOutcome(summary=f"found {len(candidates)} projects")

        return await self._jobs.submit(kind="discovery.scan", title="Scan for projects", work=work)

    @action(title="Add project")
    async def add_project(self, request: PathInput) -> Done:
        """List a directory as a project, whether or not a scan would find it."""
        directory = Path(request.path).expanduser()
        if not directory.is_dir():
            raise ActionError(f"{directory} is not a directory")
        path = str(directory.resolve())
        extra = [*dict.fromkeys([*self.config.extra_projects, path])]
        ignored = [item for item in self.config.ignored if str(Path(item)) != path]
        await self.update_config(self.config.model_copy(update={"extra_projects": extra, "ignored": ignored}))
        return Done(message=f"added {path}")

    @action(title="Remove from dashboard", confirm=True)
    async def remove_project(self, request: PathInput) -> Done:
        """Stop listing a project. Nothing on disk changes."""
        path = str(Path(request.path))
        extra = [item for item in self.config.extra_projects if str(Path(item)) != path]
        ignored = [*dict.fromkeys([*self.config.ignored, path])]
        await self.update_config(self.config.model_copy(update={"extra_projects": extra, "ignored": ignored}))
        return Done(message=f"removed {path}")

    @action(title="Browse folders", query=True)
    async def browse(self, request: BrowseInput) -> FolderListing:
        """List subfolders for the server-side folder picker; no path lists drives (or `/`)."""
        if request.path is None:
            drives = _drives()
            return FolderListing(
                path=None, parent=None, entries=[FolderEntry(name=d, path=d, is_project=False) for d in drives]
            )
        directory = Path(request.path)
        if not directory.is_dir():
            raise ActionError(f"{directory} is not a directory", status=404)

        def listing() -> list[FolderEntry]:
            entries: list[FolderEntry] = []
            with contextlib.suppress(OSError), os.scandir(directory) as found:
                for entry in sorted(found, key=lambda item: item.name.lower()):
                    with contextlib.suppress(OSError):
                        if entry.is_dir() and not entry.name.startswith("."):
                            child = Path(entry.path)
                            is_project = (child / "forge.project.toml").is_file() or (child / ".git").exists()
                            entries.append(FolderEntry(name=entry.name, path=str(child), is_project=is_project))
            return entries

        parent = directory.parent if directory.parent != directory else None
        return FolderListing(
            path=str(directory), parent=str(parent) if parent else None, entries=await asyncio.to_thread(listing)
        )

    @action(title="Clone from URL", long_running=True)
    async def clone(self, request: CloneInput) -> JobRef:
        """git clone into a chosen root, then list the clone as a project."""
        root = Path(request.root)
        if not root.is_dir():
            raise ActionError(f"{root} is not a directory")
        name = request.name or request.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if not name:
            raise ActionError("could not derive a folder name from the URL; give one")
        target = root / name
        if target.exists():
            raise ActionError(f"{target} already exists", status=409)

        async def work(context: JobContext) -> JobOutcome:
            env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
            result = await context.run(["git", "clone", "--progress", request.url, str(target)], root, env)
            if not result.ok:
                return JobOutcome(status=JobStatus.FAILED, summary=f"git clone exited {result.exit_code}")
            await self.add_project(PathInput(path=str(target)))
            return JobOutcome(summary=f"cloned into {target}")

        return await self._jobs.submit(kind="discovery.clone", title=f"Clone {name}", work=work)
