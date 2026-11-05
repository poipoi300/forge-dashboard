"""Keep a set of services running, in dependency order, and know when to stop trying.

One supervisor serves two hosts: the TUI runner supervises the dashboard's own
services, and the API supervises project services declared in manifests. It
knows nothing about either; a `ServiceSpec` carries everything host-specific,
including `plan`, which turns the resolved port and the providers' endpoints
into a concrete command.

Each service owns one asyncio task for its whole life: dependencies first,
pre-steps, port resolution, spawn, readiness, output pumping, and the restart
decision from `policy`. A user stop sets `stop_requested` before terminating,
so the exit that follows is never counted as a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, NoReturn, Protocol

import httpx
from pydantic import BaseModel

from forge_dashboard.process import ports, tree
from forge_dashboard.supervisor import policy
from forge_dashboard.supervisor.policy import ACTIVE_STATES, ServiceState

LOG_LINES_KEPT = 2000
READINESS_POLL_S = 0.25
DEPENDENCY_POLL_S = 0.1


@dataclass(frozen=True)
class PortRequest:
    """How a service gets its port."""

    policy: Literal["dynamic", "pinned"]
    preferred: int | None = None
    pinned: int | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class Readiness:
    """The signal that moves a service from starting to running."""

    kind: Literal["port", "http", "log", "none"]
    path: str = "/"
    expect_status: int = 200
    pattern: str | None = None


@dataclass(frozen=True)
class Endpoint:
    """Where a service listens once its port is resolved."""

    host: str
    port: int | None

    @property
    def connect_host(self) -> str:
        """The address a local client should dial: wildcard binds are reached over loopback."""
        return {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(self.host, self.host)

    def url(self, path: str = "/") -> str | None:
        if self.port is None:
            return None
        host = f"[{self.connect_host}]" if ":" in self.connect_host else self.connect_host
        suffix = path if path.startswith("/") else f"/{path}"
        return f"http://{host}:{self.port}{suffix}"


@dataclass(frozen=True)
class LaunchPlan:
    """A concrete command, rendered from a spec once ports are known.

    Attributes:
        env: Variables layered over the supervisor's own environment.
    """

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]


@dataclass(frozen=True)
class PreStep:
    """A command that must succeed before the service starts (install, build).

    Attributes:
        is_current: Returns True when the step's output is already up to date
            (a content-hash stamp matches), so the step is skipped.
        mark_current: Called after the step succeeds, to record the stamp.
    """

    label: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] | None = None
    is_current: Callable[[], bool] | None = None
    mark_current: Callable[[], None] | None = None


PlanFn = Callable[[Endpoint, Mapping[str, Endpoint]], LaunchPlan]


@dataclass(frozen=True)
class ServiceSpec:
    """Everything the supervisor needs to run one service.

    Attributes:
        owner_root: Directory that identifies our own processes: a leftover
            listener whose cwd or cmdline lies inside it may be reclaimed.
        plan: Renders the command from the resolved endpoint and the
            endpoints of `depends_on` providers.
    """

    name: str
    description: str
    host: str
    port: PortRequest | None
    readiness: Readiness
    owner_root: Path
    plan: PlanFn
    depends_on: tuple[str, ...] = ()
    pre_steps: tuple[PreStep, ...] = ()
    autorestart: bool = True
    open_path: str | None = None


class SupervisorEventKind(StrEnum):
    READY = "ready"
    CRASHED = "crashed"
    FAILED = "failed"
    STOPPED = "stopped"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SupervisorEvent:
    service: str
    kind: SupervisorEventKind
    detail: str


class PortHolderInfo(BaseModel):
    """A foreign process holding a pinned port."""

    pid: int
    name: str
    port: int


class ServiceStatus(BaseModel):
    """A snapshot of one service. Times are wall-clock epoch seconds so displays tick on their own clock."""

    name: str
    description: str
    state: ServiceState
    detail: str
    pid: int | None
    host: str
    port: int | None
    url: str | None
    started_at: float | None
    ready_at: float | None
    retry_at: float | None
    restarts: int
    autorestart: bool
    blocked_by: PortHolderInfo | None


class ProcessHandle(Protocol):
    """What the supervisor needs from a running child; `tree.OwnedProcess` implements it."""

    @property
    def pid(self) -> int: ...

    def is_alive(self) -> bool: ...

    def tree_pids(self) -> set[int]: ...

    def lines(self) -> AsyncIterator[str]: ...

    async def wait(self) -> int: ...

    async def stop(self, grace_s: float = ..., on_stage: Callable[[str], None] | None = None) -> None: ...


class SpawnFn(Protocol):
    def __call__(
        self, argv: tuple[str, ...], cwd: Path, env: Mapping[str, str] | None = None
    ) -> Awaitable[ProcessHandle]: ...


class StepFn(Protocol):
    def __call__(
        self, argv: tuple[str, ...], cwd: Path, env: Mapping[str, str] | None, on_line: Callable[[str], None]
    ) -> Awaitable[tree.CompletedRun]: ...


class ReadinessProbe(Protocol):
    async def __call__(self, service: SupervisedService) -> bool: ...


async def _spawn(argv: tuple[str, ...], cwd: Path, env: Mapping[str, str] | None = None) -> ProcessHandle:
    return await tree.spawn(argv, cwd, env)


async def _run_step(
    argv: tuple[str, ...], cwd: Path, env: Mapping[str, str] | None, on_line: Callable[[str], None]
) -> tree.CompletedRun:
    return await tree.stream(argv, cwd, env, on_line)


@dataclass
class SupervisedService:
    """One service and everything the supervisor knows about it."""

    spec: ServiceSpec
    state: ServiceState = ServiceState.STOPPED
    detail: str = ""
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES_KEPT))
    restarts: int = 0
    autorestart: bool = True
    endpoint: Endpoint | None = None
    handle: ProcessHandle | None = None
    task: asyncio.Task[None] | None = None
    crashes: deque[float] = field(default_factory=lambda: deque(maxlen=policy.MAX_CRASHES))
    build_failures: deque[float] = field(default_factory=lambda: deque(maxlen=policy.MAX_BUILD_FAILURES))
    stop_requested: bool = False
    started_at: float | None = None
    ready_at: float | None = None
    retry_at: float | None = None
    log_matched: bool = False
    blocked_by: ports.PortHolder | None = None
    lifecycle: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.autorestart = self.spec.autorestart

    def status(self) -> ServiceStatus:
        endpoint = self.endpoint or Endpoint(self.spec.host, None)
        return ServiceStatus(
            name=self.spec.name,
            description=self.spec.description,
            state=self.state,
            detail=self.detail,
            pid=self.handle.pid if self.handle is not None and self.handle.is_alive() else None,
            host=endpoint.host,
            port=endpoint.port,
            url=endpoint.url(self.spec.open_path or "/") if self.state is ServiceState.RUNNING else None,
            started_at=self.started_at,
            ready_at=self.ready_at,
            retry_at=self.retry_at,
            restarts=self.restarts,
            autorestart=self.autorestart,
            blocked_by=(
                PortHolderInfo(pid=self.blocked_by.pid, name=self.blocked_by.name, port=self.blocked_by.port)
                if self.blocked_by is not None
                else None
            ),
        )


class _Fatal(Exception):
    """The service cannot start at all (missing executable, blocked port); it is parked, not retried."""


class Supervisor:
    """Starts, watches, and restarts a set of services."""

    def __init__(
        self,
        specs: Iterable[ServiceSpec],
        *,
        log_tag: str = "supervisor",
        on_change: Callable[[], None] | None = None,
        on_event: Callable[[SupervisorEvent], None] | None = None,
        spawn: SpawnFn = _spawn,
        run_step: StepFn = _run_step,
        probe: ReadinessProbe | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_clock: Callable[[], float] = time.time,
        reserved_ports: Iterable[int] = (),
    ) -> None:
        self.services: dict[str, SupervisedService] = {}
        for spec in specs:
            self.add(spec)
        self._tag = f"[{log_tag}]"
        self._on_change = on_change or (lambda: None)
        self._on_event = on_event or (lambda _event: None)
        self._spawn = spawn
        self._run_step = run_step
        self._probe = probe or default_probe
        self._clock = clock
        self._sleep = sleep
        self._wall = wall_clock
        self._reserved = frozenset(reserved_ports)
        self._background: set[asyncio.Task[None]] = set()

    # --- registry ---

    def add(self, spec: ServiceSpec) -> None:
        """Register a service; replacing a stopped one with a new spec is allowed."""
        existing = self.services.get(spec.name)
        if existing is not None and existing.state in ACTIVE_STATES:
            raise RuntimeError(f"service {spec.name!r} is running; stop it before replacing its spec")
        self.services[spec.name] = SupervisedService(spec=spec)

    # --- queries ---

    def snapshot(self) -> list[ServiceStatus]:
        return [service.status() for service in self.services.values()]

    def status(self, name: str) -> ServiceStatus:
        return self.services[name].status()

    def log_lines(self, name: str) -> list[str]:
        return list(self.services[name].lines)

    def any_active(self) -> bool:
        return any(service.state in ACTIVE_STATES for service in self.services.values())

    # --- commands ---

    async def start(self, name: str) -> None:
        """Start a service (and its dependencies); a manual start clears all failure history."""
        service = self.services[name]
        async with service.lifecycle:
            if service.state in ACTIVE_STATES:
                return
            service.crashes.clear()
            service.build_failures.clear()
            service.restarts = 0
            service.stop_requested = False
            service.blocked_by = None
            # Active before the task first runs, so a dependent waiting on it never sees "stopped".
            self._set(service, ServiceState.STARTING, "starting")
            service.task = asyncio.create_task(self._supervise(service), name=f"supervise:{name}")

    async def start_all(self) -> None:
        await asyncio.gather(*(self.start(name) for name in self.services))

    async def stop(self, name: str) -> None:
        """Stop a service and prevent the restart its exit would otherwise trigger."""
        service = self.services[name]
        async with service.lifecycle:
            service.stop_requested = True
            if service.task is not None and not service.task.done():
                service.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await service.task
            service.task = None
            service.retry_at = None
            await self._terminate(service)
            service.started_at = None
            service.ready_at = None
            self._set(service, ServiceState.STOPPED, "stopped")
            self._emit(service, SupervisorEventKind.STOPPED, "stopped")

    async def stop_all(self, timeout_s: float = 20.0) -> None:
        """Stop every service concurrently under one bounded timeout."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(self.stop(name) for name in self.services), return_exceptions=True), timeout_s
            )

    async def restart(self, name: str) -> None:
        await self.stop(name)
        await self.start(name)

    async def release_and_start(self, name: str) -> None:
        """After explicit confirmation: kill the foreign holder of a pinned port, then start."""
        service = self.services[name]
        holder = service.blocked_by
        if holder is not None:
            await asyncio.to_thread(ports.reclaim, holder)
        await self.start(name)

    def toggle_autorestart(self, name: str) -> bool:
        service = self.services[name]
        service.autorestart = not service.autorestart
        self._on_change()
        return service.autorestart

    def write_log(self, name: str, log_dir: Path) -> Path:
        """Write a service's retained output to `<log_dir>/<service>.log`."""
        service = self.services[name]
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{name}.log"
        stamp = datetime.now(tz=UTC).isoformat()
        path.write_text(f"# {name}, captured {stamp}\n" + "\n".join(service.lines) + "\n", encoding="utf-8")
        return path

    async def wait_ready(self, name: str) -> bool:
        """Wait until the service runs (True) or settles in a non-active state (False)."""
        service = self.services[name]
        while True:
            if service.state is ServiceState.RUNNING:
                return True
            if service.state not in ACTIVE_STATES:
                return False
            await asyncio.sleep(DEPENDENCY_POLL_S)

    # --- supervision loop ---

    async def _supervise(self, service: SupervisedService) -> None:
        delay: float | None = None
        build_delay: float | None = None
        first_attempt = True
        try:
            await self._start_dependencies(service)
            while True:
                if not first_attempt:
                    service.restarts += 1
                first_attempt = False

                if not await self._run_pre_steps(service):
                    service.build_failures.append(self._clock())
                    if not policy.should_retry_build(service.build_failures, self._clock(), service.autorestart):
                        attempts = len(service.build_failures)
                        self._set(service, ServiceState.BUILD_FAILED, f"build failed; gave up after {attempts} tries")
                        self._emit(service, SupervisorEventKind.FAILED, service.detail)
                        return
                    build_delay = policy.next_delay(build_delay)
                    await self._backoff(service, build_delay, "build failed")
                    continue
                build_delay = None
                service.build_failures.clear()

                exit_code, uptime_s = await self._run_once(service)
                if service.stop_requested:
                    return
                if policy.run_was_healthy(uptime_s):
                    service.crashes.clear()
                    delay = None
                service.crashes.append(self._clock())
                reason = f"exited with code {exit_code}"
                self._emit(service, SupervisorEventKind.CRASHED, reason)
                if not policy.should_restart(service.crashes, self._clock(), service.autorestart):
                    count = len(service.crashes)
                    self._set(service, ServiceState.FAILED, f"{reason}; gave up after {count} crashes")
                    self._emit(service, SupervisorEventKind.FAILED, service.detail)
                    return
                delay = policy.next_delay(delay)
                await self._backoff(service, delay, reason)
        except _Fatal:
            return

    async def _start_dependencies(self, service: SupervisedService) -> None:
        for dependency in service.spec.depends_on:
            await self.start(dependency)
        for dependency in service.spec.depends_on:
            self._set(service, ServiceState.STARTING, f"waiting for {dependency}")
            if not await self.wait_ready(dependency):
                detail = f"dependency {dependency!r} is {self.services[dependency].state}"
                self._set(service, ServiceState.FAILED, detail)
                self._emit(service, SupervisorEventKind.FAILED, detail)
                raise _Fatal(detail)

    async def _backoff(self, service: SupervisedService, delay_s: float, reason: str) -> None:
        self._log(service, f"{reason}; retrying in {delay_s:.0f}s")
        service.retry_at = self._wall() + delay_s
        self._set(service, ServiceState.BACKOFF, f"{reason}; retrying")
        await self._sleep(delay_s)
        service.retry_at = None

    async def _run_pre_steps(self, service: SupervisedService) -> bool:
        for step in service.spec.pre_steps:
            if step.is_current is not None and await asyncio.to_thread(step.is_current):
                self._log(service, f"{step.label}: up to date, skipped")
                continue
            self._set(service, ServiceState.BUILDING, step.label)
            self._log(service, f"$ {' '.join(step.argv)}")
            try:
                result = await self._run_step(
                    step.argv, step.cwd, tree.child_env(step.env), lambda line: self._append(service, line)
                )
            except tree.ExecutableNotFoundError as error:
                self._park(service, ServiceState.FAILED, str(error))
            if not result.ok:
                self._log(service, f"{step.label} failed with code {result.exit_code}")
                return False
            if step.mark_current is not None:
                await asyncio.to_thread(step.mark_current)
        return True

    def _park(self, service: SupervisedService, state: ServiceState, detail: str) -> NoReturn:
        self._log(service, detail)
        self._set(service, state, detail)
        self._emit(
            service,
            SupervisorEventKind.BLOCKED if state is ServiceState.BLOCKED else SupervisorEventKind.FAILED,
            detail,
        )
        raise _Fatal(detail)

    async def _resolve_endpoint(self, service: SupervisedService) -> Endpoint:
        request = service.spec.port
        host = service.spec.host
        if request is None:
            return Endpoint(host, None)
        if request.policy == "dynamic":
            taken = self._reserved | {
                other.endpoint.port
                for other in self.services.values()
                if other is not service and other.endpoint is not None and other.endpoint.port is not None
            }
            return Endpoint(host, await asyncio.to_thread(ports.allocate_port, request.preferred, taken))
        if request.pinned is None:
            raise ValueError(f"{service.spec.name}: pinned port policy without a port")
        port = request.pinned
        for holder in await asyncio.to_thread(ports.holders, [port]):
            if holder.lives_inside(service.spec.owner_root) and holder.pid not in ports.protected_pids():
                self._log(service, f"reclaiming our leftover {holder.name} (pid {holder.pid}) on port {port}")
                await asyncio.to_thread(ports.reclaim, holder)
                continue
            service.blocked_by = holder
            self._park(service, ServiceState.BLOCKED, f"port {port} is held by {holder.name} (pid {holder.pid})")
        if await asyncio.to_thread(ports.port_has_listener, port):
            self._park(service, ServiceState.BLOCKED, f"port {port} is still in use")
        return Endpoint(host, port)

    async def _run_once(self, service: SupervisedService) -> tuple[int, float]:
        """Spawn, watch readiness, and pump output until exit; returns (exit code, uptime)."""
        previous_port = service.endpoint.port if service.endpoint is not None else None
        service.endpoint = await self._resolve_endpoint(service)
        providers = {
            name: self.services[name].endpoint or Endpoint("127.0.0.1", None) for name in service.spec.depends_on
        }
        plan = service.spec.plan(service.endpoint, providers)
        self._set(service, ServiceState.STARTING, "spawning")
        try:
            handle = await self._spawn(plan.argv, plan.cwd, tree.child_env(plan.env))
        except tree.ExecutableNotFoundError as error:
            self._park(service, ServiceState.FAILED, str(error))
        service.handle = handle
        service.started_at = self._wall()
        service.ready_at = None
        service.log_matched = False
        started = self._clock()
        self._log(
            service,
            f"started pid {handle.pid}" + (f" on port {service.endpoint.port}" if service.endpoint.port else ""),
        )
        readiness = asyncio.create_task(self._watch_readiness(service, previous_port))
        pattern = re.compile(service.spec.readiness.pattern) if service.spec.readiness.pattern else None
        try:
            async for line in handle.lines():
                self._append(service, line)
                if pattern is not None and pattern.search(line):
                    service.log_matched = True
            exit_code = await handle.wait()
        finally:
            readiness.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await readiness
        uptime = self._clock() - started
        service.started_at = None
        service.ready_at = None
        return exit_code, uptime

    async def _watch_readiness(self, service: SupervisedService, previous_port: int | None) -> None:
        while service.handle is not None and service.handle.is_alive():
            if await self._probe(service):
                service.ready_at = self._wall()
                self._set(service, ServiceState.RUNNING, f"pid {service.handle.pid}")
                self._emit(service, SupervisorEventKind.READY, service.detail)
                current_port = service.endpoint.port if service.endpoint is not None else None
                if previous_port is not None and current_port != previous_port:
                    await self._restart_dependents(service.spec.name)
                return
            await asyncio.sleep(READINESS_POLL_S)

    async def _restart_dependents(self, provider: str) -> None:
        for other in self.services.values():
            if provider in other.spec.depends_on and other.state in ACTIVE_STATES:
                self._log(other, f"{provider} moved to a new port; restarting")
                task = asyncio.create_task(self.restart(other.spec.name), name=f"restart-dependent:{other.spec.name}")
                self._background.add(task)
                task.add_done_callback(self._background.discard)

    async def _terminate(self, service: SupervisedService) -> None:
        handle, service.handle = service.handle, None
        if handle is None:
            return
        self._set(service, ServiceState.STOPPING, "stopping")
        await handle.stop(on_stage=lambda message: self._log(service, message))

    # --- bookkeeping ---

    def _set(self, service: SupervisedService, state: ServiceState, detail: str) -> None:
        service.state = state
        service.detail = detail
        self._on_change()

    def _append(self, service: SupervisedService, line: str) -> None:
        service.lines.append(line)
        self._on_change()

    def _log(self, service: SupervisedService, message: str) -> None:
        self._append(service, f"{self._tag} {message}")

    def _emit(self, service: SupervisedService, kind: SupervisorEventKind, detail: str) -> None:
        self._on_event(SupervisorEvent(service=service.spec.name, kind=kind, detail=detail))


async def default_probe(service: SupervisedService) -> bool:
    """Evaluate a service's readiness signal once."""
    readiness = service.spec.readiness
    handle = service.handle
    endpoint = service.endpoint
    if handle is None:
        return False
    if readiness.kind == "none":
        return True
    if readiness.kind == "log":
        return service.log_matched
    if endpoint is None or endpoint.port is None:
        return False
    if readiness.kind == "port":
        owned = await asyncio.to_thread(handle.tree_pids)
        return any(
            listener.port == endpoint.port and listener.pid in owned
            for listener in await asyncio.to_thread(ports.listeners)
        )
    url = endpoint.url(readiness.path)
    if url is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return False
    return response.status_code == readiness.expect_status
