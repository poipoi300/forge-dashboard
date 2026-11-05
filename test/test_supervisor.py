"""Supervisor lifecycle: restart policy, user stops, builds, dependencies, ports, readiness."""

import asyncio
import socket
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from forge_dashboard.supervisor import policy
from forge_dashboard.supervisor.core import (
    LOG_LINES_KEPT,
    Endpoint,
    LaunchPlan,
    PortRequest,
    PreStep,
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
