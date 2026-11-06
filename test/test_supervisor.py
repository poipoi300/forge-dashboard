"""Supervisor lifecycle: restart policy, user stops, builds, dependencies, ports, readiness."""

import asyncio
import logging
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path

import psutil
import pytest

from forge_dashboard.process import ports, tree
from forge_dashboard.supervisor import policy
from forge_dashboard.supervisor.core import (
    LOG_LINES_KEPT,
    Endpoint,
    LaunchPlan,
    PortRequest,
    PreStep,
    ProcessHandle,
    Readiness,
    ServiceSpec,
    Supervisor,
    SupervisorEvent,
    SupervisorEventKind,
)
from forge_dashboard.supervisor.policy import ServiceState

PY = sys.executable

# --- pure policy ---


def test_backoff_doubles_from_one_second_and_caps_at_thirty() -> None:
    delays: list[float] = []
    delay: float | None = None
    for _ in range(7):
        delay = policy.next_delay(delay)
        delays.append(delay)
    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_five_crashes_in_a_minute_stop_restarts() -> None:
    assert policy.should_restart([0, 1, 2, 3], now=4, autorestart=True)
    assert not policy.should_restart([0, 1, 2, 3, 4], now=5, autorestart=True)
    assert policy.should_restart([0, 1, 2, 3, 4], now=61, autorestart=True)


def test_autorestart_off_never_restarts() -> None:
    assert not policy.should_restart([], now=0, autorestart=False)
    assert not policy.should_retry_build([], now=0, autorestart=False)


def test_build_failures_have_their_own_smaller_budget() -> None:
    assert policy.should_retry_build([0, 1], now=2, autorestart=True)
    assert not policy.should_retry_build([0, 1, 2], now=3, autorestart=True)
    assert policy.should_retry_build([0, 1, 2], now=125, autorestart=True)


def test_thirty_seconds_of_uptime_counts_as_healthy() -> None:
    assert policy.run_was_healthy(30.0)
    assert not policy.run_was_healthy(29.9)


# --- supervisor against real short-lived processes ---


class FakeTime:
    """A monotonic clock that sleeps instantly by advancing itself."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def _plan(argv: list[str], cwd: Path, env: Mapping[str, str] | None = None) -> Callable[..., LaunchPlan]:
    def plan(endpoint: Endpoint, providers: Mapping[str, Endpoint]) -> LaunchPlan:
        rendered = [part.replace("{port}", str(endpoint.port)) for part in argv]
        provider_env = {f"PORT_{name.upper()}": str(ep.port) for name, ep in providers.items()}
        return LaunchPlan(argv=tuple(rendered), cwd=cwd, env={**dict(env or {}), **provider_env})

    return plan


def _spec(name: str, argv: list[str], cwd: Path, **overrides: object) -> ServiceSpec:
    fields: dict[str, object] = {
        "name": name,
        "description": f"{name} service",
        "host": "127.0.0.1",
        "port": None,
        "readiness": Readiness(kind="none"),
        "owner_root": cwd,
        "plan": _plan(argv, cwd),
    }
    fields.update(overrides)
    return ServiceSpec(**fields)  # type: ignore[arg-type]


async def _settle(supervisor: Supervisor, name: str, states: set[ServiceState], timeout_s: float = 20) -> None:
    async def wait() -> None:
        while supervisor.services[name].state not in states:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait(), timeout_s)


async def test_crash_loop_parks_as_failed_after_five_crashes(tmp_path: Path) -> None:
    fake = FakeTime()
    events: list[SupervisorEvent] = []
    spec = _spec("crashy", [PY, "-c", "import sys; sys.exit(3)"], tmp_path)
    supervisor = Supervisor([spec], clock=fake.clock, sleep=fake.sleep, on_event=events.append)
    await supervisor.start("crashy")
    await _settle(supervisor, "crashy", {ServiceState.FAILED})
    status = supervisor.status("crashy")
    assert status.restarts == 4
    assert "gave up after 5 crashes" in status.detail
    assert [event.kind for event in events].count(SupervisorEventKind.CRASHED) == 5


async def test_user_stop_is_not_a_crash(tmp_path: Path) -> None:
    events: list[SupervisorEvent] = []
    spec = _spec("sleeper", [PY, "-c", "import time; time.sleep(60)"], tmp_path)
    supervisor = Supervisor([spec], on_event=events.append)
    await supervisor.start("sleeper")
    await _settle(supervisor, "sleeper", {ServiceState.RUNNING})
    await supervisor.stop("sleeper")
    await asyncio.sleep(0.3)
    assert supervisor.status("sleeper").state is ServiceState.STOPPED
    assert SupervisorEventKind.CRASHED not in [event.kind for event in events]


async def test_build_failure_parks_without_spending_the_crash_budget(tmp_path: Path) -> None:
    fake = FakeTime()
    step = PreStep(label="build", argv=(PY, "-c", "import sys; sys.exit(1)"), cwd=tmp_path)
    spec = _spec("built", [PY, "-c", "print('never')"], tmp_path, pre_steps=(step,))
    supervisor = Supervisor([spec], clock=fake.clock, sleep=fake.sleep)
    await supervisor.start("built")
    await _settle(supervisor, "built", {ServiceState.BUILD_FAILED})
    service = supervisor.services["built"]
    assert len(service.crashes) == 0
    assert len(service.build_failures) == policy.MAX_BUILD_FAILURES


async def test_current_pre_steps_are_skipped_and_successful_ones_stamped(tmp_path: Path) -> None:
    stamped: list[str] = []
    skipped = PreStep(label="install", argv=("never-run-xyz",), cwd=tmp_path, is_current=lambda: True)
    built = PreStep(
        label="build", argv=(PY, "-c", "print('built')"), cwd=tmp_path, mark_current=lambda: stamped.append("build")
    )
    spec = _spec("app", [PY, "-c", "import time; time.sleep(60)"], tmp_path, pre_steps=(skipped, built))
    supervisor = Supervisor([spec], log_tag="runner")
    await supervisor.start("app")
    await _settle(supervisor, "app", {ServiceState.RUNNING})
    logs = supervisor.log_lines("app")
    assert "[runner] install: up to date, skipped" in logs
    assert "built" in logs
    assert stamped == ["build"]
    await supervisor.stop_all()


async def test_missing_executable_parks_as_failed_without_retrying(tmp_path: Path) -> None:
    spec = _spec("ghost", ["definitely-not-installed-xyz"], tmp_path)
    supervisor = Supervisor([spec])
    await supervisor.start("ghost")
    await _settle(supervisor, "ghost", {ServiceState.FAILED})
    assert "not on PATH" in supervisor.status("ghost").detail
    assert supervisor.status("ghost").restarts == 0


LISTENER = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n"
    "print('listening', flush=True)\n"
    "time.sleep(60)\n"
)


async def test_dependencies_start_first_and_their_port_flows_to_dependents(tmp_path: Path) -> None:
    backend = _spec(
        "backend",
        [PY, "-c", LISTENER, "{port}"],
        tmp_path,
        port=PortRequest(policy="dynamic"),
        readiness=Readiness(kind="port"),
    )
    frontend_script = "import os, time; print('backend at', os.environ['PORT_BACKEND'], flush=True); time.sleep(60)"
    frontend = _spec(
        "frontend",
        [PY, "-c", frontend_script],
        tmp_path,
        depends_on=("backend",),
        readiness=Readiness(kind="log", pattern=r"backend at \d+"),
    )
    supervisor = Supervisor([backend, frontend])
    await supervisor.start("frontend")
    await _settle(supervisor, "frontend", {ServiceState.RUNNING})
    backend_port = supervisor.status("backend").port
    assert supervisor.status("backend").state is ServiceState.RUNNING
    assert f"backend at {backend_port}" in supervisor.log_lines("frontend")
    assert supervisor.status("backend").url == f"http://127.0.0.1:{backend_port}/"
    await supervisor.stop_all()
    assert {s.state for s in supervisor.snapshot()} == {ServiceState.STOPPED}


async def test_pinned_port_held_by_a_foreign_process_blocks(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as foreign:
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        port = foreign.getsockname()[1]
        spec = _spec(
            "pinned",
            [PY, "-c", LISTENER, "{port}"],
            tmp_path / "elsewhere",
            port=PortRequest(policy="pinned", pinned=port, evidence="test"),
        )
        events: list[SupervisorEvent] = []
        supervisor = Supervisor([spec], on_event=events.append)
        await supervisor.start("pinned")
        await _settle(supervisor, "pinned", {ServiceState.BLOCKED})
        status = supervisor.status("pinned")
        assert status.blocked_by is not None and status.blocked_by.port == port
        assert events[-1].kind is SupervisorEventKind.BLOCKED


async def test_http_readiness_waits_for_the_expected_status(tmp_path: Path) -> None:
    server = (
        "import http.server, sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200 if self.path == '/health' else 404); self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n"
    )
    spec = _spec(
        "web",
        [PY, "-c", server, "{port}"],
        tmp_path,
        port=PortRequest(policy="dynamic"),
        readiness=Readiness(kind="http", path="/health"),
        open_path="/ui",
    )
    supervisor = Supervisor([spec])
    await supervisor.start("web")
    await _settle(supervisor, "web", {ServiceState.RUNNING})
    status = supervisor.status("web")
    assert status.url is not None and status.url.endswith("/ui")
    log_dir = tmp_path / "logs"
    assert supervisor.write_log("web", log_dir).read_text(encoding="utf-8").startswith("# web, captured")
    await supervisor.stop("web")


async def test_replacing_a_running_spec_is_refused(tmp_path: Path) -> None:
    spec = _spec("sleeper", [PY, "-c", "import time; time.sleep(60)"], tmp_path)
    supervisor = Supervisor([spec])
    await supervisor.start("sleeper")
    await _settle(supervisor, "sleeper", {ServiceState.RUNNING})
    with pytest.raises(RuntimeError, match="stop it"):
        supervisor.add(spec)
    assert supervisor.toggle_autorestart("sleeper") is False
    await supervisor.stop("sleeper")
    supervisor.add(spec)


def test_endpoint_urls_dial_loopback_for_wildcard_binds() -> None:
    assert Endpoint("0.0.0.0", 8080).url("x") == "http://127.0.0.1:8080/x"
    assert Endpoint("::", 8080).url() == "http://[::1]:8080/"
    assert Endpoint("127.0.0.1", None).url() is None


async def test_log_cursor_keeps_reading_after_the_ring_buffer_fills(tmp_path: Path) -> None:
    count = LOG_LINES_KEPT + 300
    script = f"import time\nfor i in range({count}): print(f'line {{i}}', flush=True)\ntime.sleep(60)"
    supervisor = Supervisor([_spec("chatty", [PY, "-c", script], tmp_path)])
    await supervisor.start("chatty")

    async def all_written() -> None:
        while supervisor.services["chatty"].lines_written < count:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(all_written(), 20)
    lines, first, cursor = supervisor.log_since("chatty", 0)
    newer, _, unchanged = supervisor.log_since("chatty", cursor)
    await supervisor.stop_all()

    assert len(lines) == LOG_LINES_KEPT
    assert first == cursor - LOG_LINES_KEPT
    assert lines[-1] == f"line {count - 1}"
    assert (newer, unchanged) == ([], cursor)


# --- a failure inside supervision never strands a service ---


async def test_a_missing_working_directory_fails_the_service_and_start_works_again(tmp_path: Path) -> None:
    cwd = tmp_path / "not-created-yet"
    events: list[SupervisorEvent] = []
    spec = _spec("lost", [PY, "-c", "import time; time.sleep(60)"], cwd)
    supervisor = Supervisor([spec], log_tag="runner", on_event=events.append)
    await supervisor.start("lost")
    await _settle(supervisor, "lost", {ServiceState.FAILED})
    assert f"cannot start {PY!r} in {cwd}" in supervisor.status("lost").detail
    assert events[-1].kind is SupervisorEventKind.FAILED
    assert any(line.startswith("[runner] cannot start") for line in supervisor.log_lines("lost"))

    cwd.mkdir()
    await supervisor.start("lost")
    await _settle(supervisor, "lost", {ServiceState.RUNNING})
    await supervisor.stop("lost")


async def test_a_missing_working_directory_fails_a_pre_step_without_retrying(tmp_path: Path) -> None:
    step = PreStep(label="install", argv=(PY, "-c", "print(1)"), cwd=tmp_path / "gone")
    supervisor = Supervisor([_spec("built", [PY, "-c", "print(2)"], tmp_path, pre_steps=(step,))])
    await supervisor.start("built")
    await _settle(supervisor, "built", {ServiceState.FAILED})
    assert "cannot run" in supervisor.status("built").detail
    assert len(supervisor.services["built"].build_failures) == 0


async def test_an_unexpected_error_while_supervising_fails_the_service_and_start_works_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[int] = []

    def plan(endpoint: Endpoint, providers: Mapping[str, Endpoint]) -> LaunchPlan:
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("cannot render the command")
        return LaunchPlan(argv=(PY, "-c", "import time; time.sleep(60)"), cwd=tmp_path, env={})

    events: list[SupervisorEvent] = []
    supervisor = Supervisor([_spec("fragile", [], tmp_path, plan=plan)], log_tag="runner", on_event=events.append)
    with caplog.at_level(logging.ERROR, logger="forge_dashboard.supervisor"):
        await supervisor.start("fragile")
        await _settle(supervisor, "fragile", {ServiceState.FAILED})
    assert supervisor.status("fragile").detail == "ValueError: cannot render the command"
    assert "[runner] unexpected error: ValueError: cannot render the command" in supervisor.log_lines("fragile")
    assert [event.kind for event in events] == [SupervisorEventKind.FAILED]
    assert "supervising fragile failed" in caplog.text

    await supervisor.start("fragile")
    await _settle(supervisor, "fragile", {ServiceState.RUNNING})
    await supervisor.stop("fragile")


async def test_a_dependency_missing_from_the_registry_fails_the_dependent(tmp_path: Path) -> None:
    orphan = _spec("orphan", [PY, "-c", "print(1)"], tmp_path, depends_on=("ghost",))
    supervisor = Supervisor([orphan])
    await supervisor.start("orphan")
    await _settle(supervisor, "orphan", {ServiceState.FAILED})
    assert supervisor.status("orphan").detail == "KeyError: 'ghost'"


async def test_an_error_after_the_child_spawned_does_not_leave_it_running(tmp_path: Path) -> None:
    spawned: list[tree.OwnedProcess] = []

    async def spawn(argv: tuple[str, ...], cwd: Path, env: Mapping[str, str] | None = None) -> ProcessHandle:
        process = await tree.spawn(argv, cwd, env)
        spawned.append(process)
        return process

    raised = False

    def on_line(service: str, line: str) -> None:
        nonlocal raised
        if "started pid" in line and not raised:
            raised = True
            raise RuntimeError("the line sink broke")

    spec = _spec("sleeper", [PY, "-c", "import time; time.sleep(60)"], tmp_path)
    supervisor = Supervisor([spec], spawn=spawn, on_line=on_line)
    await supervisor.start("sleeper")
    await _settle(supervisor, "sleeper", {ServiceState.FAILED})
    assert supervisor.status("sleeper").detail == "RuntimeError: the line sink broke"
    assert not spawned[0].is_alive()


# --- stopping many services reports the ones that would not stop ---


class StuckProcess:
    """A child that never exits by itself; `stop` does whatever the test says."""

    pid = 2_000_000_000

    def __init__(self, stop: Callable[[], Awaitable[None]]) -> None:
        self._stop = stop
        self._exited = asyncio.Event()

    def is_alive(self) -> bool:
        return not self._exited.is_set()

    def tree_pids(self) -> set[int]:
        return set()

    async def lines(self) -> AsyncIterator[str]:
        await self._exited.wait()
        remaining: list[str] = []
        for line in remaining:
            yield line

    async def wait(self) -> int:
        await self._exited.wait()
        return 0

    async def stop(self, grace_s: float = 0.0, on_stage: Callable[[str], None] | None = None) -> None:
        await self._stop()


def _supervisor_with_a_stuck_service(
    tmp_path: Path, stop: Callable[[], Awaitable[None]], on_event: Callable[[SupervisorEvent], None] | None = None
) -> Supervisor:
    async def spawn(argv: tuple[str, ...], cwd: Path, env: Mapping[str, str] | None = None) -> ProcessHandle:
        if argv[0] == "stuck":
            return StuckProcess(stop)
        return await tree.spawn(argv, cwd, env)

    specs = [
        _spec("stuck", ["stuck"], tmp_path),
        _spec("healthy", [PY, "-c", "import time; time.sleep(60)"], tmp_path),
    ]
    return Supervisor(specs, spawn=spawn, log_tag="runner", on_event=on_event)


async def test_stop_all_reports_a_service_that_fails_to_stop_and_stops_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode() -> None:
        raise RuntimeError("the tree is stuck")

    events: list[SupervisorEvent] = []
    supervisor = _supervisor_with_a_stuck_service(tmp_path, explode, events.append)
    await supervisor.start_all()
    for name in ("stuck", "healthy"):
        await _settle(supervisor, name, {ServiceState.RUNNING})

    with caplog.at_level(logging.ERROR, logger="forge_dashboard.supervisor"):
        failed = await supervisor.stop_all()

    assert failed == ["stuck"]
    assert supervisor.status("healthy").state is ServiceState.STOPPED
    status = supervisor.status("stuck")
    assert status.state is ServiceState.FAILED and status.detail == "stop failed: RuntimeError: the tree is stuck"
    assert "[runner] stop failed: RuntimeError: the tree is stuck" in supervisor.log_lines("stuck")
    assert any(event.service == "stuck" and event.kind is SupervisorEventKind.FAILED for event in events)
    assert "stuck could not be stopped" in caplog.text


async def test_stop_all_reports_a_service_still_stopping_when_the_timeout_ends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def never_finishes() -> None:
        await asyncio.sleep(3600)

    supervisor = _supervisor_with_a_stuck_service(tmp_path, never_finishes)
    await supervisor.start_all()
    for name in ("stuck", "healthy"):
        await _settle(supervisor, name, {ServiceState.RUNNING})

    with caplog.at_level(logging.ERROR, logger="forge_dashboard.supervisor"):
        failed = await supervisor.stop_all(timeout_s=1)

    assert failed == ["stuck"]
    assert supervisor.status("healthy").state is ServiceState.STOPPED
    assert "[runner] stop timed out: still stopping after 1s" in supervisor.log_lines("stuck")
    assert supervisor.status("stuck").state is ServiceState.FAILED
    assert "still stopping after 1s" in caplog.text


async def test_stop_all_of_healthy_services_reports_nothing_failed(tmp_path: Path) -> None:
    supervisor = Supervisor([_spec("sleeper", [PY, "-c", "import time; time.sleep(60)"], tmp_path)])
    await supervisor.start("sleeper")
    await _settle(supervisor, "sleeper", {ServiceState.RUNNING})
    assert await supervisor.stop_all() == []


# --- a pinned port is only ever freed from a process we can prove is ours ---


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


async def _foreign_listener(cwd: Path, port: int) -> asyncio.subprocess.Process:
    """A process of the same project that the supervisor never started (a developer's own dev server)."""
    process = await asyncio.create_subprocess_exec(
        PY, "-c", LISTENER, str(port), cwd=cwd, stdout=asyncio.subprocess.PIPE
    )
    assert process.stdout is not None
    await process.stdout.readline()
    return process


def _pinned_spec(tmp_path: Path, port: int) -> ServiceSpec:
    return _spec(
        "pinned",
        [PY, "-c", LISTENER, "{port}"],
        tmp_path,
        port=PortRequest(policy="pinned", pinned=port, evidence="test"),
    )


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
    await process.wait()


async def test_a_listener_of_the_same_project_that_we_did_not_spawn_blocks_the_start_and_survives(
    tmp_path: Path,
) -> None:
    port = _free_port()
    foreign = await _foreign_listener(tmp_path, port)
    try:
        supervisor = Supervisor([_pinned_spec(tmp_path, port)])
        await supervisor.start("pinned")
        await _settle(supervisor, "pinned", {ServiceState.BLOCKED})
        blocked_by = supervisor.status("pinned").blocked_by
        assert blocked_by is not None and blocked_by.port == port
        assert psutil.Process(blocked_by.pid).is_running()
        assert foreign.returncode is None

        await supervisor.release_and_start("pinned")
        await _settle(supervisor, "pinned", {ServiceState.RUNNING})
        await asyncio.wait_for(foreign.wait(), 10)
        await supervisor.stop("pinned")
    finally:
        await _kill(foreign)


async def test_a_restart_does_not_kill_a_listener_we_did_not_spawn_either(tmp_path: Path) -> None:
    port = _free_port()
    supervisor = Supervisor([_pinned_spec(tmp_path, port)])
    await supervisor.start("pinned")
    await _settle(supervisor, "pinned", {ServiceState.RUNNING})
    await supervisor.stop("pinned")
    foreign = await _foreign_listener(tmp_path, port)
    try:
        await supervisor.restart("pinned")
        await _settle(supervisor, "pinned", {ServiceState.BLOCKED})
        assert foreign.returncode is None
    finally:
        await _kill(foreign)


async def test_a_leftover_recorded_as_spawned_by_us_is_reclaimed_on_start(tmp_path: Path) -> None:
    port = _free_port()
    leftover = await _foreign_listener(tmp_path, port)
    try:
        supervisor = Supervisor([_pinned_spec(tmp_path, port)])
        [holder] = ports.holders([port])
        supervisor.services["pinned"].spawned[holder.pid] = holder.create_time
        await supervisor.start("pinned")
        await _settle(supervisor, "pinned", {ServiceState.RUNNING})
        assert any("reclaiming our leftover" in line for line in supervisor.log_lines("pinned"))
        await asyncio.wait_for(leftover.wait(), 10)
        await supervisor.stop("pinned")
    finally:
        await _kill(leftover)


async def test_a_reused_pid_is_not_mistaken_for_our_leftover(tmp_path: Path) -> None:
    port = _free_port()
    stranger = await _foreign_listener(tmp_path, port)
    try:
        supervisor = Supervisor([_pinned_spec(tmp_path, port)])
        [holder] = ports.holders([port])
        supervisor.services["pinned"].spawned[holder.pid] = holder.create_time - 100.0  # an older process, same pid
        await supervisor.start("pinned")
        await _settle(supervisor, "pinned", {ServiceState.BLOCKED})
        assert stranger.returncode is None
    finally:
        await _kill(stranger)


async def test_the_processes_of_a_running_service_are_remembered_as_ours(tmp_path: Path) -> None:
    supervisor = Supervisor([_spec("sleeper", [PY, "-c", "import time; time.sleep(60)"], tmp_path)])
    await supervisor.start("sleeper")
    await _settle(supervisor, "sleeper", {ServiceState.RUNNING})
    service = supervisor.services["sleeper"]
    assert service.handle is not None
    root = service.handle.pid
    assert service.spawned[root] == psutil.Process(root).create_time()
    await supervisor.stop("sleeper")


async def test_a_running_service_cannot_be_removed_but_a_stopped_one_can(tmp_path: Path) -> None:
    supervisor = Supervisor([_spec("sleeper", [PY, "-c", "import time; time.sleep(60)"], tmp_path)])
    await supervisor.start("sleeper")
    await _settle(supervisor, "sleeper", {ServiceState.RUNNING})
    with pytest.raises(RuntimeError, match="stop it before removing"):
        supervisor.remove("sleeper")
    await supervisor.stop("sleeper")
    supervisor.remove("sleeper")
    assert supervisor.snapshot() == []
