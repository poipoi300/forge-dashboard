"""Launch and supervise the services projects declare, and watch their deployments.

Each project gets its own `Supervisor` (the same one the TUI runner uses for
the dashboard itself) built from its manifest's `[[services]]`: port policies,
readiness signals, dependency order with resolved ports exported to
dependents, backoff restarts, and log ring buffers. Instances already running
outside the dashboard (a listener on the service's port whose process lives
inside the project) show as `running (external)`, not as stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, ClassVar, Literal, override

import httpx
from pydantic import BaseModel, Field

from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, Done, JobRef, NoInput, ProjectRef, Service, action
from forge_dashboard.core.tasks import BackgroundTasks, repeat
from forge_dashboard.core.ui import ui
from forge_dashboard.domain.commands import render_command
from forge_dashboard.domain.summary import (
    SERVICE_STATE_UI,
    DeploymentBrief,
    ServiceBrief,
    ServicesFacts,
    ServiceStateName,
)
from forge_dashboard.manifest.models import HttpReady, LogReady, NoReady, PinnedPort, PortReady
from forge_dashboard.manifest.models import Service as ManifestService
from forge_dashboard.process import ports
from forge_dashboard.services.discovery import ChangeKind
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService
from forge_dashboard.supervisor.core import (
    Endpoint,
    LaunchPlan,
    PortRequest,
    Readiness,
    ServiceSpec,
    Supervisor,
    SupervisorEvent,
    SupervisorEventKind,
)
from forge_dashboard.supervisor.policy import ACTIVE_STATES, ServiceState

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

LAUNCH_TIMEOUT_S = 180
RESERVED_PORTS = (8000, 8317)


def env_key(service_id: str) -> str:
    """`FORGE_SERVICE_PORT_<ID>` suffix: the id upper-cased with `-` as `_`."""
    return service_id.upper().replace("-", "_")


class ExternalInstance(BaseModel):
    pid: int
    process: str
    port: int


class ProjectServiceStatus(BaseModel):
    project_id: str
    service_id: str
    label: str
    description: str | None = None
    state: ServiceStateName = Field(json_schema_extra=SERVICE_STATE_UI)
    detail: str = ""
    pid: int | None = None
    host: str
    port: int | None = None
    url: str | None = Field(None, json_schema_extra=ui(format="url"))
    started_at: float | None = Field(None, json_schema_extra=ui(format="relative-time", short_label="up since"))
    restarts: int = 0
    machine: str
    owner: Literal["dashboard", "external", "none"] = Field(
        "none", json_schema_extra=ui(format="badge", tones={"dashboard": "info", "external": "warn", "none": "neutral"})
    )
    external: ExternalInstance | None = None
    blocked_by: str | None = None


class DeploymentStatus(BaseModel):
    project_id: str
    deployment_id: str
    label: str
    url: str = Field(json_schema_extra=ui(format="url"))
    machine: str | None = None
    environment: str = Field(json_schema_extra=ui(format="badge"))
    health: Literal["up", "down", "unchecked"] = Field(
        "unchecked", json_schema_extra=ui(format="badge", tones={"up": "good", "down": "bad", "unchecked": "neutral"})
    )
    status_code: int | None = None
    checked_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    error: str | None = None


class ProjectServicesConfig(BaseModel):
    external_scan_s: int = Field(
        10, ge=2, le=600, json_schema_extra=ui(widget="duration", help="How often to look for external instances.")
    )
    deployment_check_s: int = Field(60, ge=10, le=3600, json_schema_extra=ui(widget="duration"))


class ProjectServicesState(BaseModel):
    services: list[ProjectServiceStatus] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["label", "project_id", "state", "host", "port", "started_at", "owner"]),
    )
    deployments: list[DeploymentStatus] = Field(
        default_factory=list, json_schema_extra=ui(columns=["label", "project_id", "health", "machine", "url"])
    )


class ServiceRef(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    service_id: str


class LogsInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    service_id: str
    offset: int = Field(0, ge=0)


class ServiceLogs(BaseModel):
    lines: list[str] = Field(json_schema_extra=ui(format="code"))
    next_offset: int
    total: int


class LaunchResult(BaseModel):
    url: str | None = Field(None, json_schema_extra=ui(format="url"))
    service_id: str


class ServiceCrashed(Event):
    type: ClassVar[str] = "services.crashed"
    persist: ClassVar[bool] = True
    notify_kind: ClassVar[str | None] = "service-crash"
    service_id: str
    detail: str

    def title(self) -> str:
        return f"Service {self.service_id} {self.detail}"

    def severity(self) -> Severity:
        return "error"


class ServiceStarted(Event):
    type: ClassVar[str] = "services.ready"
    persist: ClassVar[bool] = True
    service_id: str
    url: str | None

    def title(self) -> str:
        return f"Service {self.service_id} ready" + (f" at {self.url}" if self.url else "")


STATE_NAMES: dict[ServiceState, ServiceStateName] = {
    ServiceState.STOPPED: "stopped",
    ServiceState.BUILDING: "building",
    ServiceState.STARTING: "starting",
    ServiceState.RUNNING: "running",
    ServiceState.BACKOFF: "backoff",
    ServiceState.STOPPING: "stopping",
    ServiceState.FAILED: "failed",
    ServiceState.BUILD_FAILED: "build failed",
    ServiceState.BLOCKED: "blocked",
}


def _port_request(service: ManifestService) -> PortRequest | None:
    if service.port is None:
        return None
    if isinstance(service.port, PinnedPort):
        return PortRequest(policy="pinned", pinned=service.port.port, evidence=service.port.evidence)
    return PortRequest(policy="dynamic", preferred=service.port.preferred)


def _readiness(service: ManifestService) -> Readiness:
    ready = service.effective_ready
    if isinstance(ready, HttpReady):
        return Readiness(kind="http", path=ready.path, expect_status=ready.expect_status)
    if isinstance(ready, LogReady):
        return Readiness(kind="log", pattern=ready.pattern)
    if isinstance(ready, PortReady):
        return Readiness(kind="port")
    assert isinstance(ready, NoReady)
    return Readiness(kind="none")


class ProjectServicesService(Service[ProjectServicesConfig, ProjectServicesState], ProjectListener):
    id = "services"
    title = "Services"
    description = (
        "Launches and supervises the services projects declare; detects external instances; checks deployments."
    )
    Config = ProjectServicesConfig
    State = ProjectServicesState
    events = (ServiceCrashed, ServiceStarted)

    def __init__(self, runtime: Runtime, projects: ProjectsService, jobs: JobsService) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._jobs = jobs
        self._supervisors: dict[str, Supervisor] = {}
        self._external: dict[tuple[str, str], ExternalInstance] = {}
        self._deployments: dict[tuple[str, str], DeploymentStatus] = {}
        self._background = BackgroundTasks()
        self._dirty = False
        projects.listen(self)

    # --- lifecycle ---

    @override
    async def start(self) -> None:
        self._background.spawn(
            repeat(self._scan_external, lambda: self.config.external_scan_s, label="external service scan"),
            "services-external",
        )
        self._background.spawn(
            repeat(self._check_deployments, lambda: self.config.deployment_check_s, label="deployment checks"),
            "services-deployments",
        )

    @override
    async def stop(self) -> None:
        await self._background.cancel_all()
        await asyncio.gather(*(supervisor.stop_all() for supervisor in self._supervisors.values()))

    # --- registry hooks ---

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        services = record.manifest.services if record.manifest is not None else []
        supervisor = self._supervisors.get(record.id)
        if supervisor is None:
            if not services:
                self._publish()
                return
            supervisor = Supervisor(
                [],
                log_tag="dashboard",
                on_change=self._changed,
                on_event=self._event_handler(record.id),
                reserved_ports=RESERVED_PORTS,
            )
            self._supervisors[record.id] = supervisor
        for service in services:
            existing = supervisor.services.get(service.id)
            if existing is None or existing.state not in ACTIVE_STATES:
                supervisor.add(self._spec(record, service))
        self._publish()

    @override
    async def project_removed(self, project_id: str) -> None:
        supervisor = self._supervisors.pop(project_id, None)
        if supervisor is not None:
            await supervisor.stop_all()
        self._publish()

    @override
    async def files_changed(self, record: ProjectRecord, kinds: frozenset[ChangeKind]) -> None:
        if "manifest" in kinds:
            await self.project_upserted(record)

    def _spec(self, record: ProjectRecord, service: ManifestService) -> ServiceSpec:
        def plan(endpoint: Endpoint, providers: Mapping[str, Endpoint]) -> LaunchPlan:
            provider_env: dict[str, str] = {}
            for provider_id, provider in providers.items():
                if provider.port is not None:
                    provider_env[f"FORGE_SERVICE_PORT_{env_key(provider_id)}"] = str(provider.port)
                    provider_env[f"FORGE_SERVICE_URL_{env_key(provider_id)}"] = provider.base_url or ""
            port_value = str(endpoint.port) if endpoint.port is not None else ""
            command = render_command(
                argv=service.argv,
                cwd=service.cwd,
                env=service.env,
                project_root=record.path,
                project_id=record.id,
                dashboard_url=self.runtime.dashboard_url,
                values={"port": port_value, "host": endpoint.host},
                extra_env={"FORGE_SERVICE_PORT": port_value, "FORGE_SERVICE_HOST": endpoint.host, **provider_env},
            )
            return LaunchPlan(argv=command.argv, cwd=command.cwd, env=command.env)

        return ServiceSpec(
            name=service.id,
            description=service.description or service.label,
            host=service.host,
            port=_port_request(service),
            readiness=_readiness(service),
            owner_root=record.path,
            plan=plan,
            depends_on=tuple(service.depends_on),
            open_path=service.open,
        )

    # --- state ---

    def _changed(self) -> None:
        # Supervisors report every log line; coalesce into one state publish per loop turn.
        if self._dirty:
            return
        self._dirty = True
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().call_soon(self._publish)

    def _event_handler(self, project_id: str) -> Callable[[SupervisorEvent], None]:
        def handle(event: SupervisorEvent) -> None:
            self._supervisor_event(project_id, event)

        return handle

    def _supervisor_event(self, project_id: str, event: SupervisorEvent) -> None:
        if event.kind in (SupervisorEventKind.CRASHED, SupervisorEventKind.FAILED, SupervisorEventKind.BLOCKED):
            self.publish(ServiceCrashed(project_id=project_id, service_id=event.service, detail=event.detail))
        elif event.kind is SupervisorEventKind.READY:
            status = self._supervisors[project_id].status(event.service)
            self.publish(ServiceStarted(project_id=project_id, service_id=event.service, url=status.url))

    def _statuses(self) -> list[ProjectServiceStatus]:
        statuses: list[ProjectServiceStatus] = []
        machine = self._projects.config.machine_label
        for record in self._projects.records():
            if record.manifest is None:
                continue
            supervisor = self._supervisors.get(record.id)
            for service in record.manifest.services:
                supervised = supervisor.services.get(service.id) if supervisor is not None else None
                status = supervised.status() if supervised is not None else None
                external = self._external.get((record.id, service.id))
                state: ServiceStateName = STATE_NAMES[status.state] if status is not None else "stopped"
                owner: Literal["dashboard", "external", "none"] = (
                    "dashboard" if status and status.state in ACTIVE_STATES else "none"
                )
                url, port, pid, started = (
                    (status.url, status.port, status.pid, status.started_at) if status else (None, None, None, None)
                )
                if external is not None and (status is None or status.state not in ACTIVE_STATES):
                    state, owner, port, pid = "external", "external", external.port, external.pid
                    url = (
                        Endpoint(service.host, external.port).url(service.open or "/")
                        if service.open is not None
                        else None
                    )
                statuses.append(
                    ProjectServiceStatus(
                        project_id=record.id,
                        service_id=service.id,
                        label=service.label,
                        description=service.description,
                        state=state,
                        detail=status.detail if status is not None else "",
                        pid=pid,
                        host=service.host,
                        port=port,
                        url=url,
                        started_at=started,
                        restarts=status.restarts if status is not None else 0,
                        machine=machine,
                        owner=owner,
                        external=external,
                        blocked_by=(
                            f"{status.blocked_by.name} (pid {status.blocked_by.pid})"
                            if status is not None and status.blocked_by is not None
                            else None
                        ),
                    )
                )
        return statuses

    def _publish(self) -> None:
        self._dirty = False
        statuses = self._statuses()
        by_project: dict[str, list[ServiceBrief]] = {}
        for status in statuses:
            address = f"{status.host}:{status.port}" if status.port is not None else None
            by_project.setdefault(status.project_id, []).append(
                ServiceBrief(
                    id=status.service_id,
                    label=status.label,
                    state=status.state,
                    address=address,
                    url=status.url,
                    machine=status.machine,
                )
            )
        deployments: dict[str, list[DeploymentBrief]] = {}
        for deployment in self._deployments.values():
            deployments.setdefault(deployment.project_id, []).append(
                DeploymentBrief(
                    id=deployment.deployment_id,
                    label=deployment.label,
                    url=deployment.url,
                    machine=deployment.machine,
                    environment=deployment.environment,
                    health=deployment.health,
                )
            )
        for record in self._projects.records():
            if record.manifest is not None and (record.manifest.services or record.manifest.deployments):
                facts = ServicesFacts(
                    services=by_project.get(record.id, []), deployments=deployments.get(record.id, [])
                )
                self._projects.put(record.id, facts)
        self.set_state(
            ProjectServicesState(
                services=statuses,
                deployments=sorted(self._deployments.values(), key=lambda d: (d.project_id, d.deployment_id)),
            )
        )

    def url_for(self, project_id: str, service_id: str) -> str | None:
        """Base URL of a running (supervised or external) service, for HTTP metrics and actions."""
        for status in self._statuses():
            matches = (status.project_id, status.service_id) == (project_id, service_id)
            if matches and status.port is not None and status.state in ("running", "external"):
                return Endpoint(status.host, status.port).base_url
        return None

    # --- external instances and deployments ---

    async def _scan_external(self) -> None:
        found: dict[tuple[str, str], ExternalInstance] = {}
        for record in self._projects.records():
            if record.manifest is None:
                continue
            supervisor = self._supervisors.get(record.id)
            owned: set[int] = set()
            if supervisor is not None:
                for supervised in supervisor.services.values():
                    if supervised.handle is not None:
                        owned |= await asyncio.to_thread(supervised.handle.tree_pids)
            for service in record.manifest.services:
                port = _known_port(service)
                if port is None:
                    continue
                for holder in await asyncio.to_thread(ports.holders, [port]):
                    if holder.pid not in owned and holder.lives_inside(record.path):
                        found[(record.id, service.id)] = ExternalInstance(
                            pid=holder.pid, process=holder.name, port=port
                        )
        if found != self._external:
            self._external = found
            self._publish()

    async def _check_deployments(self) -> None:
        results: dict[tuple[str, str], DeploymentStatus] = {}
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for record in self._projects.records():
                for deployment in record.manifest.deployments if record.manifest is not None else []:
                    base = DeploymentStatus(
                        project_id=record.id,
                        deployment_id=deployment.id,
                        label=deployment.label,
                        url=str(deployment.url),
                        machine=deployment.machine,
                        environment=deployment.environment,
                    )
                    if deployment.health is None:
                        results[(record.id, deployment.id)] = base
                        continue
                    target = str(deployment.url).rstrip("/") + "/" + deployment.health.path.lstrip("/")
                    try:
                        response = await client.get(target)
                    except httpx.HTTPError as error:
                        results[(record.id, deployment.id)] = base.model_copy(
                            update={"health": "down", "checked_at": time.time(), "error": str(error)}
                        )
                        continue
                    healthy = response.status_code == deployment.health.expect_status
                    results[(record.id, deployment.id)] = base.model_copy(
                        update={
                            "health": "up" if healthy else "down",
                            "status_code": response.status_code,
                            "checked_at": time.time(),
                        }
                    )
        self._deployments = results
        self._publish()

    # --- actions ---

    def _supervisor(self, project_id: str, service_id: str) -> Supervisor:
        record = self._projects.record(project_id)
        supervisor = self._supervisors.get(record.id)
        if supervisor is None or service_id not in supervisor.services:
            raise ActionError(f"{project_id} declares no service {service_id!r}", status=404)
        return supervisor

    @action(title="Start service")
    async def start_service(self, request: ServiceRef) -> Done:
        """Start a service and, first, the services it depends on."""
        await self._supervisor(request.project_id, request.service_id).start(request.service_id)
        return Done(message="starting")

    @action(title="Stop service")
    async def stop_service(self, request: ServiceRef) -> Done:
        """Stop a service and its whole process tree."""
        await self._supervisor(request.project_id, request.service_id).stop(request.service_id)
        return Done(message="stopped")

    @action(title="Restart service")
    async def restart_service(self, request: ServiceRef) -> Done:
        """Stop then start a service; a dynamic port may change, and dependents follow."""
        await self._supervisor(request.project_id, request.service_id).restart(request.service_id)
        return Done(message="restarted")

    @action(title="Free port and start", danger=True, confirm=True)
    async def release_port(self, request: ServiceRef) -> Done:
        """Kill the foreign process holding this service's pinned port, then start the service."""
        await self._supervisor(request.project_id, request.service_id).release_and_start(request.service_id)
        return Done(message="starting")

    @action(title="Launch", long_running=True)
    async def launch(self, request: ProjectRef) -> JobRef:
        """Start the primary service with its dependencies, wait until it is ready, and return its URL."""
        record = self._projects.record(request.project_id)
        manifest = record.manifest
        if manifest is None or not manifest.services:
            raise ActionError("this project declares no services", status=409)
        primary = manifest.launch.primary_service if manifest.launch and manifest.launch.primary_service else None
        service_id = primary or manifest.services[0].id
        supervisor = self._supervisor(record.id, service_id)

        async def work(context: JobContext) -> JobOutcome:
            external = self._external.get((record.id, service_id))
            if external is not None:
                url = self.url_for(record.id, service_id)
                context.log(f"{service_id} is already running outside the dashboard (pid {external.pid})")
                return JobOutcome(summary=url or "already running", result=LaunchResult(url=url, service_id=service_id))
            await supervisor.start(service_id)
            context.log(f"starting {service_id} and its dependencies")
            try:
                ready = await asyncio.wait_for(supervisor.wait_ready(service_id), LAUNCH_TIMEOUT_S)
            except TimeoutError:
                ready = False
            status = supervisor.status(service_id)
            for line in supervisor.log_lines(service_id)[-40:]:
                context.log(line)
            if not ready:
                return JobOutcome(status=JobStatus.FAILED, summary=f"{service_id} is {status.state}: {status.detail}")
            return JobOutcome(summary=status.url or "ready", result=LaunchResult(url=status.url, service_id=service_id))

        return await self._jobs.submit(
            kind="services.launch", title=f"Launch {record.id}", work=work, project_id=record.id
        )

    @action(title="Stop all services", confirm=True)
    async def stop_all(self, _request: NoInput) -> Done:
        """Stop every service the dashboard started, in every project."""
        await asyncio.gather(*(supervisor.stop_all() for supervisor in self._supervisors.values()))
        return Done(message="stopped all")

    @action(title="Service log", query=True)
    async def logs(self, request: LogsInput) -> ServiceLogs:
        """The retained output of a service (ANSI colours kept)."""
        lines = self._supervisor(request.project_id, request.service_id).log_lines(request.service_id)
        return ServiceLogs(lines=lines[request.offset :], next_offset=len(lines), total=len(lines))

    @action(title="Check deployments")
    async def check_deployments(self, _request: NoInput) -> ProjectServicesState:
        """Probe every deployment's health endpoint now."""
        await self._check_deployments()
        return self.state

    @action(title="Scan for external instances")
    async def scan_external(self, _request: NoInput) -> ProjectServicesState:
        """Look for services already running outside the dashboard."""
        await self._scan_external()
        return self.state


def _known_port(service: ManifestService) -> int | None:
    """The port an external instance would be on: pinned, else the dynamic preference."""
    if service.port is None:
        return None
    if isinstance(service.port, PinnedPort):
        return service.port.port
    return service.port.preferred
