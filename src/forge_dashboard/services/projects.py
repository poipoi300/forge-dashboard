"""The project registry and the overview it feeds.

Discovery says which directories are projects; this service turns each into a
`ProjectRecord` (id, path, role, manifest), tells every interested service
when a project appears, changes, or disappears, collects their facts, and
composes the `ProjectSummary` list the overview renders. User-owned state
(status marker, notes, favorites, per-project settings) lives here too.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import socket
import time
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

from pydantic import BaseModel, Field

from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, Done, NoInput, ProjectRef, Service, action
from forge_dashboard.core.ui import ui
from forge_dashboard.domain import launch as launch_rules
from forge_dashboard.domain import summary as rules
from forge_dashboard.domain.launch import LaunchGuide
from forge_dashboard.domain.summary import (
    AgentFacts,
    CheckFacts,
    CiFacts,
    ForkFacts,
    GitFacts,
    IssueFacts,
    Lifecycle,
    ManifestFacts,
    MetricsFacts,
    ProjectFacts,
    ProjectIdentity,
    ProjectRole,
    ProjectSummary,
    ServicesFacts,
    StandardsFacts,
    UserProjectState,
)
from forge_dashboard.manifest import ManifestIssue, ManifestLoad, ProjectManifest
from forge_dashboard.manifest.models import MANIFEST_FILENAME
from forge_dashboard.services.discovery import Candidate, ChangeKind, DiscoveryService
from forge_dashboard.services.manifests import ManifestsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")

Facts = (
    ManifestFacts
    | GitFacts
    | CheckFacts
    | CiFacts
    | IssueFacts
    | ServicesFacts
    | ForkFacts
    | StandardsFacts
    | MetricsFacts
    | AgentFacts
)
_FACT_FIELDS: dict[type[BaseModel], str] = {
    ManifestFacts: "manifest",
    GitFacts: "git",
    CheckFacts: "checks",
    CiFacts: "ci",
    IssueFacts: "issues",
    ServicesFacts: "services",
    ForkFacts: "fork",
    StandardsFacts: "standards",
    MetricsFacts: "metrics",
    AgentFacts: "agent",
}


@dataclass(frozen=True)
class ProjectRecord:
    """A project as the other services see it."""

    id: str
    path: Path
    role: ProjectRole
    manifest: ProjectManifest | None
    manifest_issues: tuple[ManifestIssue, ...]
    has_manifest_file: bool
    is_git: bool

    @property
    def default_branch(self) -> str:
        return self.manifest.project.default_branch if self.manifest is not None else "main"

    @property
    def languages(self) -> list[str]:
        return list(self.manifest.project.languages) if self.manifest is not None else []


class ProjectListener:
    """Hooks a service implements to follow the registry. Each must return quickly."""

    async def project_upserted(self, record: ProjectRecord) -> None:
        """A project appeared, or its manifest changed."""

    async def project_removed(self, project_id: str) -> None:
        """A project is no longer listed."""

    async def files_changed(self, record: ProjectRecord, kinds: frozenset[ChangeKind]) -> None:
        """The watcher saw changes of these kinds inside the project."""


def slugify(name: str) -> str:
    slug = _NOT_SLUG.sub("-", name.lower()).strip("-")
    return slug or "project"


def raw_manifest_id(directory: Path) -> str | None:
    """`[project].id` from a manifest that may fail validation, when it is at least a valid slug."""
    try:
        data = tomllib.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    project = data.get("project")
    candidate = project.get("id") if isinstance(project, dict) else None
    return candidate if isinstance(candidate, str) and _SLUG.match(candidate) else None


def _manifest_mtime(directory: Path) -> float | None:
    try:
        return (directory / MANIFEST_FILENAME).stat().st_mtime
    except FileNotFoundError:
        return None


def assign_ids(wanted: Iterable[tuple[Path, str]]) -> dict[Path, str]:
    """Give every path its wanted id; later duplicates (by path order) get a stable path-hash suffix."""
    assigned: dict[Path, str] = {}
    taken: set[str] = set()
    for path, project_id in sorted(wanted, key=lambda pair: str(pair[0]).lower()):
        final = project_id
        if final in taken:
            final = f"{project_id}-{hashlib.sha1(str(path).lower().encode()).hexdigest()[:6]}"
        taken.add(final)
        assigned[path] = final
    return assigned


class OverviewTotals(BaseModel):
    projects: int = Field(0, json_schema_extra=ui(short_label="projects"))
    failing: int = Field(0, json_schema_extra=ui(short_label="failing"))
    attention: int = Field(0, json_schema_extra=ui(short_label="need attention"))
    running_services: int = Field(0, json_schema_extra=ui(short_label="services up"))


class ProjectsConfig(BaseModel):
    machine_label: str = Field(
        default_factory=socket.gethostname, json_schema_extra=ui(help="How this machine is labelled on projects.")
    )
    poll_s: int = Field(
        30,
        ge=5,
        le=3600,
        json_schema_extra=ui(
            widget="duration", advanced=True, help="Fallback check for manifest edits the watcher missed."
        ),
    )


class ProjectsState(BaseModel):
    projects: list[ProjectSummary] = Field(
        default_factory=list,
        json_schema_extra=ui(
            columns=[
                "name",
                "status",
                "health",
                "last_change",
                "v1",
                "checks",
                "ci",
                "issues",
                "git",
                "services",
                "standards",
                "agent",
            ]
        ),
    )
    totals: OverviewTotals = Field(default_factory=OverviewTotals)
    hidden: list[str] = Field(default_factory=list, json_schema_extra=ui(help="Projects hidden from the overview."))
    loaded: bool = Field(False, json_schema_extra=ui(hidden=True))


class ProjectSettings(BaseModel):
    """Per-project overrides of dashboard behaviour."""

    hidden: bool = Field(False, json_schema_extra=ui(widget="toggle", help="Hide from the overview."))
    editor_command: list[str] = Field(
        default_factory=list,
        json_schema_extra=ui(widget="tags", placeholder="zed {path}", help="Overrides the editor for this project."),
    )


class StatusInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    status: Lifecycle = Field(json_schema_extra=ui(widget="select"))
    note: str | None = Field(None, json_schema_extra=ui(widget="textarea", placeholder="Why, or what is next"))


class FavoriteInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    favorite: bool


class NotesInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    markdown: str = Field(json_schema_extra=ui(widget="textarea"))


class Notes(BaseModel):
    markdown: str
    updated_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))


class SettingsInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    settings: ProjectSettings


class ProjectLinks(BaseModel):
    github: str | None = Field(None, json_schema_extra=ui(format="url"))
    homepage: str | None = Field(None, json_schema_extra=ui(format="url"))
    docs: str | None = Field(None, json_schema_extra=ui(format="url"))


class ProjectDetail(BaseModel):
    summary: ProjectSummary
    manifest: ProjectManifest | None
    manifest_issues: list[ManifestIssue]
    has_manifest_file: bool
    is_git: bool
    links: ProjectLinks
    settings: ProjectSettings


class ProjectList(BaseModel):
    projects: list[ProjectSummary]


class StatusChanged(Event):
    type: ClassVar[str] = "projects.status-changed"
    persist: ClassVar[bool] = True
    status: Lifecycle
    note: str | None

    def title(self) -> str:
        return f"Status set to {self.status.value}" + (f": {self.note}" if self.note else "")

    def severity(self) -> Severity:
        return "warning" if self.status is Lifecycle.BLOCKED else "info"


class ProjectsService(Service[ProjectsConfig, ProjectsState]):
    id = "projects"
    title = "Projects"
    description = "The project registry, user status markers and notes, and the composed overview."
    Config = ProjectsConfig
    State = ProjectsState
    events = (StatusChanged,)

    def __init__(self, runtime: Runtime, discovery: DiscoveryService, manifests: ManifestsService) -> None:
        super().__init__(runtime)
        self._discovery = discovery
        self._manifests = manifests
        self._listeners: list[ProjectListener] = []
        self._records: dict[str, ProjectRecord] = {}
        self._by_path: dict[Path, str] = {}
        self._facts: dict[str, ProjectFacts] = {}
        self._settings: dict[str, ProjectSettings] = {}
        self._sync_lock = asyncio.Lock()
        self._manifest_mtimes: dict[str, float | None] = {}
        self._poll_task: asyncio.Task[None] | None = None
        discovery.on_candidates(self._candidates_changed)
        discovery.on_change(self._files_changed)

    def listen(self, listener: ProjectListener) -> None:
        self._listeners.append(listener)

    @override
    async def start(self) -> None:
        self._poll_task = asyncio.create_task(self._poll_loop(), name="projects-poll")

    @override
    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_s)
            await self.poll_once()

    async def poll_once(self) -> list[str]:
        """Reload every manifest whose modification time changed since it was last read; returns their ids."""
        changed = [
            record for record in self.records() if _manifest_mtime(record.path) != self._manifest_mtimes.get(record.id)
        ]
        for record in changed:
            await self._files_changed(record.path, frozenset({"manifest"}))
        return [record.id for record in changed]

    # --- queries for other services ---

    def records(self) -> list[ProjectRecord]:
        return list(self._records.values())

    def record(self, project_id: str) -> ProjectRecord:
        try:
            return self._records[project_id]
        except KeyError:
            raise ActionError(f"no project {project_id!r}", status=404) from None

    def summary(self, project_id: str) -> ProjectSummary:
        return rules.compose_summary(self._facts[self.record(project_id).id])

    def settings(self, project_id: str) -> ProjectSettings:
        return self._settings.get(project_id, ProjectSettings())

    @property
    def forge_repo(self) -> Path | None:
        return self._discovery.forge_repo

    def put(self, project_id: str, facts: Facts) -> None:
        """Replace one source's facts for a project and recompose its summary."""
        current = self._facts.get(project_id)
        if current is None:
            return
        update: dict[str, object] = {_FACT_FIELDS[type(facts)]: facts}
        if isinstance(facts, IssueFacts):
            update["v1"] = self._v1(self._records[project_id], facts)
        self._facts[project_id] = current.model_copy(update=update)
        self._publish()

    # --- registry maintenance ---

    async def _candidates_changed(self, candidates: list[Candidate], _forge: Path | None) -> None:
        async with self._sync_lock:
            loaded = {Path(c.path): (c, await self._manifests.load(Path(c.path))) for c in candidates}
            wanted = [(path, self._wanted_id(path, load)) for path, (_c, load) in loaded.items()]
            ids = assign_ids(wanted)
            fresh: dict[str, ProjectRecord] = {}
            for path, (candidate, load) in loaded.items():
                fresh[ids[path]] = self._make_record(ids[path], path, candidate.kind, load)
            removed = set(self._records) - set(fresh)
            for project_id in removed:
                self._records.pop(project_id, None)
                self._facts.pop(project_id, None)
                self._manifests.forget(project_id)
                for listener in self._listeners:
                    await listener.project_removed(project_id)
            for record in fresh.values():
                await self._upsert(record, loaded[record.path][1])
            self._by_path = {record.path: project_id for project_id, record in self._records.items()}
            self._publish(loaded=True)

    def _wanted_id(self, path: Path, load: ManifestLoad) -> str:
        if load.manifest is not None:
            return load.manifest.project.id
        return raw_manifest_id(path) or slugify(path.name)

    def _make_record(self, project_id: str, path: Path, role: ProjectRole, load: ManifestLoad) -> ProjectRecord:
        has_file = (path / MANIFEST_FILENAME).is_file()
        return ProjectRecord(
            id=project_id,
            path=path,
            role=role,
            manifest=load.manifest,
            manifest_issues=tuple(load.issues) if has_file else (),
            has_manifest_file=has_file,
            is_git=(path / ".git").exists(),
        )

    async def _upsert(self, record: ProjectRecord, load: ManifestLoad) -> None:
        previous = self._records.get(record.id)
        self._records[record.id] = record
        self._manifest_mtimes[record.id] = _manifest_mtime(record.path)
        if record.has_manifest_file:
            self._manifests.record(record.id, load)
        user = await self._load_user(record.id)
        self._settings[record.id] = await self._load_settings(record.id)
        existing = self._facts.get(record.id)
        identity = self._identity(record)
        manifest_facts = (
            ManifestFacts(
                valid=load.valid,
                error_count=len(load.issues),
                first_error=load.issues[0].render(MANIFEST_FILENAME) if load.issues else None,
            )
            if record.has_manifest_file
            else None
        )
        base = existing or ProjectFacts(identity=identity)
        self._facts[record.id] = base.model_copy(
            update={"identity": identity, "user": user, "manifest": manifest_facts, "v1": self._v1(record, base.issues)}
        )
        if previous != record:
            for listener in self._listeners:
                await listener.project_upserted(record)

    def _identity(self, record: ProjectRecord) -> ProjectIdentity:
        manifest = record.manifest
        return ProjectIdentity(
            id=record.id,
            name=manifest.project.name if manifest is not None else record.path.name,
            path=str(record.path),
            role=record.role,
            summary=manifest.project.summary if manifest is not None else None,
            kind=manifest.project.kind if manifest is not None else None,
            languages=list(manifest.project.languages) if manifest is not None else [],
            tags=list(manifest.project.tags) if manifest is not None else [],
            machine=self.config.machine_label,
        )

    def _v1(self, record: ProjectRecord, issues: IssueFacts | None) -> rules.V1Progress | None:
        manifest = record.manifest
        if manifest is None:
            return None
        today = date.today()
        if manifest.v1.source == "milestones":
            milestones = [(m.title, m.weight, m.done) for m in manifest.v1.milestones]
            return rules.v1_progress(milestones, manifest.v1.target_date, today)
        if issues is None or issues.milestone_open is None or issues.milestone_closed is None:
            return rules.V1Progress(target_date=manifest.v1.target_date, caption="waiting for GitHub")
        return rules.github_milestone_progress(
            issues.milestone_open, issues.milestone_closed, manifest.v1.target_date, today
        )

    async def _files_changed(self, path: Path, kinds: frozenset[ChangeKind]) -> None:
        project_id = self._by_path.get(path)
        if project_id is None:
            return
        record = self._records[project_id]
        if "manifest" in kinds:
            load = await self._manifests.load(path)
            record = self._make_record(project_id, path, record.role, load)
            await self._upsert(record, load)
            self._publish()
        for listener in self._listeners:
            await listener.files_changed(record, kinds)

    async def refresh(self, project_id: str) -> None:
        """Re-read a project's manifest and notify listeners as if it changed."""
        record = self.record(project_id)
        await self._files_changed(record.path, frozenset({"manifest", "git", "worktree"}))

    # --- user state ---

    async def _load_user(self, project_id: str) -> UserProjectState:
        stored = await self.runtime.db.kv_get("project-user", project_id)
        return UserProjectState.model_validate(stored) if stored is not None else UserProjectState()

    async def _load_settings(self, project_id: str) -> ProjectSettings:
        stored = await self.runtime.db.kv_get("project-settings", project_id)
        return ProjectSettings.model_validate(stored) if stored is not None else ProjectSettings()

    async def _save_user(self, project_id: str, user: UserProjectState) -> None:
        await self.runtime.db.kv_put("project-user", project_id, user.model_dump(mode="json"))
        self._facts[project_id] = self._facts[project_id].model_copy(update={"user": user})
        self._publish()

    def _publish(self, *, loaded: bool | None = None) -> None:
        summaries = [rules.compose_summary(facts) for facts in self._facts.values()]
        hidden_ids = {pid for pid, settings in self._settings.items() if settings.hidden}
        visible = sorted(
            (summary for summary in summaries if summary.id not in hidden_ids),
            key=lambda summary: (not summary.favorite, summary.name.lower()),
        )
        totals = OverviewTotals(
            projects=len(visible),
            failing=sum(1 for s in visible if s.health.level is rules.HealthLevel.FAILING),
            attention=sum(1 for s in visible if s.attention),
            running_services=sum(1 for s in visible for svc in s.services if svc.state in ("running", "external")),
        )
        self.set_state(
            ProjectsState(
                projects=visible,
                totals=totals,
                hidden=sorted(hidden_ids),
                loaded=self.state.loaded if loaded is None else loaded,
            )
        )

    # --- actions ---

    @action(title="Set status")
    async def set_status(self, request: StatusInput) -> Done:
        """Set the lifecycle status marker, with an optional note."""
        self.record(request.project_id)
        current = self._facts[request.project_id].user
        user = current.model_copy(update={"status": request.status, "status_note": request.note})
        await self._save_user(request.project_id, user)
        self.publish(StatusChanged(project_id=request.project_id, status=request.status, note=request.note))
        return Done(message=f"status: {request.status.value}")

    @action(title="Favorite")
    async def set_favorite(self, request: FavoriteInput) -> Done:
        """Pin a project to the top of the overview, or unpin it."""
        self.record(request.project_id)
        user = self._facts[request.project_id].user.model_copy(update={"favorite": request.favorite})
        await self._save_user(request.project_id, user)
        return Done(message="pinned" if request.favorite else "unpinned")

    @action(title="Notes", query=True)
    async def notes(self, request: ProjectRef) -> Notes:
        """The project's free-form markdown notes."""
        stored = await self.runtime.db.kv_get("project-notes", request.project_id)
        return Notes.model_validate(stored) if stored is not None else Notes(markdown="")

    @action(title="Save notes")
    async def save_notes(self, request: NotesInput) -> Notes:
        """Replace the project's notes."""
        self.record(request.project_id)
        notes = Notes(markdown=request.markdown, updated_at=time.time())
        await self.runtime.db.kv_put("project-notes", request.project_id, notes.model_dump(mode="json"))
        return notes

    @action(title="Save project settings")
    async def save_settings(self, request: SettingsInput) -> ProjectSettings:
        """Per-project overrides: visibility and editor."""
        self.record(request.project_id)
        await self.runtime.db.kv_put("project-settings", request.project_id, request.settings.model_dump(mode="json"))
        self._settings[request.project_id] = request.settings
        self._publish()
        return request.settings

    @action(title="Project detail", query=True)
    async def detail(self, request: ProjectRef) -> ProjectDetail:
        """Everything the project page needs that is not live state."""
        record = self.record(request.project_id)
        summary = self.summary(record.id)
        manifest = record.manifest
        github = summary.issues.html_url if summary.issues is not None else None
        docs = manifest.project.docs if manifest is not None else None
        return ProjectDetail(
            summary=summary,
            manifest=manifest,
            manifest_issues=list(record.manifest_issues),
            has_manifest_file=record.has_manifest_file,
            is_git=record.is_git,
            links=ProjectLinks(
                github=github,
                homepage=str(manifest.project.homepage) if manifest is not None and manifest.project.homepage else None,
                docs=docs if docs is not None and "://" in docs else None,
            ),
            settings=self.settings(record.id),
        )

    @action(title="Launch instructions", query=True)
    async def launch_guide(self, request: ProjectRef) -> LaunchGuide:
        """How to launch and use the project: `[launch].instructions`, else the README's setup or run section."""
        record = self.record(request.project_id)
        return await asyncio.to_thread(launch_rules.launch_guide, record.path, record.manifest)

    @action(title="All projects", query=True)
    async def list_all(self, _request: NoInput) -> ProjectList:
        """Every project summary, hidden ones included."""
        return ProjectList(projects=[rules.compose_summary(facts) for facts in self._facts.values()])

    @action(title="Refresh project")
    async def refresh_project(self, request: ProjectRef) -> Done:
        """Re-read the manifest and refresh every source for this project."""
        await self.refresh(request.project_id)
        return Done(message="refreshing")

    @override
    async def on_config_changed(self, previous: ProjectsConfig) -> None:
        for project_id, facts in self._facts.items():
            self._facts[project_id] = facts.model_copy(update={"identity": self._identity(self._records[project_id])})
        self._publish()
