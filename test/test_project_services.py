"""Manifest services: launch with dependencies and ports, external instances, crashes, and deployments."""

import asyncio
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import psutil
import pytest
import respx
from conftest import MANIFEST
from testing_support import wait_for

from forge_dashboard.core.events import EventEnvelope
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, Done, NoInput, ProjectRef
from forge_dashboard.process import ports
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.jobs import JobIdInput, JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.project_services import LogsInput, ProjectServicesService, ServiceRef, env_key
from forge_dashboard.services.projects import ProjectsService

PYTHON = sys.executable.replace(chr(92), "/")
SERVER = """
import http.server
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path in ("/api/health", "/") else 404)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass

print("serving", flush=True)
http.server.HTTPServer((sys.argv[1], int(sys.argv[2])), Handler).serve_forever()
"""
FRONTEND = """
import os
import time

print("backend is", os.environ["FORGE_SERVICE_URL_BACKEND"], os.environ["FORGE_PROJECT_ID"], flush=True)
time.sleep(600)
"""
LISTENER = """
import socket
import sys
import time

s = socket.socket()
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen()
print("listening", flush=True)
time.sleep(600)
"""
SERVICES = f"""
[[services]]
id = "backend"
label = "Backend"
argv = ["{PYTHON}", "scripts/server.py", "{{host}}", "{{port}}"]
port = {{ policy = "dynamic" }}
ready = {{ kind = "http", path = "/api/health" }}
open = "/"

[[services]]
id = "frontend"
label = "Frontend"
argv = ["{PYTHON}", "scripts/frontend.py"]
depends_on = ["backend"]
ready = {{ kind = "log", pattern = "backend is http" }}

[[services]]
id = "crashy"
label = "Crashy"
argv = ["{PYTHON}", "-c", "import sys; sys.exit(4)"]

[[deployments]]
id = "prod"
label = "Production"
url = "https://demo.example.com"
machine = "vps-1"
health = {{ path = "/health" }}

[[deployments]]
id = "docs"
label = "Docs"
url = "https://docs.example.com"

[launch]
primary_service = "frontend"
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
async def services_env(
    data_paths: DataPaths, tmp_path: Path
) -> AsyncIterator[tuple[ProjectServicesService, ProjectsService, DiscoveryService, JobsService, Runtime]]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    services = ProjectServicesService(runtime, projects, jobs)
    runtime.register([jobs, manifests, projects, services, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")]})
    await runtime.db.kv_put("config", "services", {"external_scan_s": 600, "deployment_check_s": 3600})
    await runtime.start()
    try:
        yield services, projects, discovery, jobs, runtime
    finally:
        await runtime.stop()


def _project(make_project: Callable[..., Path], extra_manifest: str = SERVICES) -> Path:
    scripts = {"scripts/server.py": SERVER, "scripts/frontend.py": FRONTEND, "scripts/listener.py": LISTENER}
    return make_project("Svc", manifest=MANIFEST + extra_manifest, extra=scripts)


def test_env_keys_follow_the_contract() -> None:
    assert env_key("my-api") == "MY_API"


async def test_launch_starts_dependencies_and_reports_the_address(
    services_env: tuple[ProjectServicesService, ProjectsService, DiscoveryService, JobsService, Runtime],
    make_project: Callable[..., Path],
) -> None:
    services, projects, discovery, jobs, _runtime = services_env
    await discovery.add_project(PathInput(path=str(_project(make_project))))
    ref = await services.launch(ProjectRef(project_id="svc"))
    final = await jobs.wait(ref.job_id)
    assert final.status is JobStatus.SUCCEEDED, final.summary
    by_id = {status.service_id: status for status in services.state.services}
    backend = by_id["backend"]
    assert backend.state == "running" and backend.port is not None and backend.owner == "dashboard"
    assert backend.url == f"http://127.0.0.1:{backend.port}/"
    assert by_id["frontend"].state == "running"
    logs = await services.logs(LogsInput(project_id="svc", service_id="frontend"))
    assert f"backend is http://127.0.0.1:{backend.port} svc" in logs.lines
    assert services.url_for("svc", "backend") == f"http://127.0.0.1:{backend.port}"
    briefs = {brief.id: brief for brief in projects.summary("svc").services}
    assert briefs["backend"].address == f"127.0.0.1:{backend.port}" and briefs["crashy"].state == "stopped"
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"http://127.0.0.1:{backend.port}/api/health")).status_code == 200
    detail = await jobs.get(JobIdInput(job_id=ref.job_id))
    assert detail.result == {"url": None, "service_id": "frontend"}
    await services.stop_all(NoInput())
    await wait_for(lambda: all(s.state == "stopped" for s in services.state.services))


async def test_crashes_are_published_for_notifications(
    services_env: tuple[ProjectServicesService, ProjectsService, DiscoveryService, JobsService, Runtime],
    make_project: Callable[..., Path],
) -> None:
    services, projects, discovery, _jobs, runtime = services_env
    await discovery.add_project(PathInput(path=str(_project(make_project))))
    async with runtime.bus.subscribe() as events:
        await services.start_service(ServiceRef(project_id="svc", service_id="crashy"))
        received: list[EventEnvelope] = []

        async def crashed() -> None:
            while not any(e.type == "services.crashed" for e in received):
                received.append(await events.get())

        await asyncio.wait_for(crashed(), 20)
    crash = next(e for e in received if e.type == "services.crashed")
    assert crash.notify_kind == "service-crash" and crash.project_id == "svc"
    await services.stop_service(ServiceRef(project_id="svc", service_id="crashy"))
    with pytest.raises(ActionError, match="declares no service"):
        await services.restart_service(ServiceRef(project_id="svc", service_id="ghost"))
    assert projects.summary("svc").health.level.value in ("unknown", "good", "failing")


async def test_instances_running_outside_the_dashboard_show_as_external(
    services_env: tuple[ProjectServicesService, ProjectsService, DiscoveryService, JobsService, Runtime],
    make_project: Callable[..., Path],
) -> None:
    services, projects, discovery, jobs, _runtime = services_env
    port = free_port()
    pinned = f"""
[[services]]
id = "api"
label = "API"
argv = ["{PYTHON}", "scripts/listener.py", "{{port}}"]
port = {{ policy = "pinned", port = {port}, evidence = "fixture client config" }}
open = "/"
"""
    project = _project(make_project, pinned)
    await discovery.add_project(PathInput(path=str(project)))
    outside = await asyncio.create_subprocess_exec(
        sys.executable, "scripts/listener.py", str(port), cwd=project, stdout=asyncio.subprocess.PIPE
    )
    try:
        assert outside.stdout is not None
        await outside.stdout.readline()
        await services.scan_external(NoInput())
        [api] = services.state.services
        assert (api.state, api.owner, api.port) == ("external", "external", port)
        launcher = psutil.Process(outside.pid)
        tree = {launcher.pid, *(child.pid for child in launcher.children(recursive=True))}
        assert api.external is not None and api.external.pid in tree
        assert projects.summary("svc").services[0].state == "external"
        launched = await jobs.wait((await services.launch(ProjectRef(project_id="svc"))).job_id)
        assert launched.status is JobStatus.SUCCEEDED and launched.summary == f"http://127.0.0.1:{port}"
    finally:
        outside.kill()
        await outside.wait()


async def test_deployments_are_probed_over_http(
    services_env: tuple[ProjectServicesService, ProjectsService, DiscoveryService, JobsService, Runtime],
    make_project: Callable[..., Path],
) -> None:
    services, projects, discovery, _jobs, _runtime = services_env
    await discovery.add_project(PathInput(path=str(_project(make_project))))
    with respx.mock(assert_all_called=False) as router:
        router.get("https://demo.example.com/health").mock(return_value=httpx.Response(503))
        state = await services.check_deployments(NoInput())
    by_id = {d.deployment_id: d for d in state.deployments}
    assert (by_id["prod"].health, by_id["prod"].status_code, by_id["prod"].machine) == ("down", 503, "vps-1")
    assert by_id["docs"].health == "unchecked"
    with respx.mock(assert_all_called=False) as router:
        router.get("https://demo.example.com/health").mock(return_value=httpx.Response(200))
        await services.check_deployments(NoInput())
    briefs = {d.id: d for d in projects.summary("svc").deployments}
    assert briefs["prod"].health == "up"
    with respx.mock(assert_all_called=False) as router:
        router.get("https://demo.example.com/health").mock(side_effect=httpx.ConnectError("unreachable"))
        state = await services.check_deployments(NoInput())
    assert next(d for d in state.deployments if d.deployment_id == "prod").error == "unreachable"


# --- the supervisor follows the manifest ---

ServicesEnv = tuple[ProjectServicesService, ProjectsService, DiscoveryService, JobsService, Runtime]


def _worker(service_id: str, generation: str) -> str:
    """A portless service that prints its generation, so a log shows which manifest it was started from."""
    script = f"print('generation {generation}', flush=True); import time; time.sleep(600)"
    return f"""
[[services]]
id = "{service_id}"
label = "{service_id.title()}"
argv = ["{PYTHON}", "-c", "{script}"]
"""


def _manifest_text(*extra: str) -> str:
    base: str = MANIFEST
    return base.replace("{name}", "Svc").replace("{project_id}", "svc") + "".join(extra)


def _lines_now(services: ProjectServicesService, service_id: str) -> list[str]:
    return services._supervisors["svc"].log_lines(service_id)


def _state_of(services: ProjectServicesService, service_id: str) -> str:
    return next(status.state for status in services.state.services if status.service_id == service_id)


async def _log_lines(services: ProjectServicesService, service_id: str) -> list[str]:
    return (await services.logs(LogsInput(project_id="svc", service_id=service_id))).lines


async def test_restart_runs_the_argv_the_manifest_has_now(
    services_env: ServicesEnv, make_project: Callable[..., Path]
) -> None:
    services, projects, discovery, _jobs, _runtime = services_env
    root = make_project("Svc", manifest=MANIFEST + _worker("worker", "one"))
    await discovery.add_project(PathInput(path=str(root)))
    worker = ServiceRef(project_id="svc", service_id="worker")
    await services.start_service(worker)
    await wait_for(lambda: _state_of(services, "worker") == "running")

    manifest = root / "forge.project.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("generation one", "generation two"), "utf-8")
    await projects.refresh("svc")
    assert _state_of(services, "worker") == "running"  # a running service is left alone

    await services.restart_service(worker)
    await wait_for(lambda: _state_of(services, "worker") == "running")
    await wait_for(lambda: "generation two" in _lines_now(services, "worker"))
    assert "generation one" not in _lines_now(services, "worker")
    await services.stop_service(worker)


async def test_a_service_dropped_from_the_manifest_leaves_the_supervisor_once_it_stops(
    services_env: ServicesEnv, make_project: Callable[..., Path]
) -> None:
    services, projects, discovery, _jobs, _runtime = services_env
    root = make_project("Svc", manifest=MANIFEST + _worker("worker", "one") + _worker("keeper", "one"))
    await discovery.add_project(PathInput(path=str(root)))
    worker = ServiceRef(project_id="svc", service_id="worker")
    await services.start_service(worker)
    await wait_for(lambda: _state_of(services, "worker") == "running")

    (root / "forge.project.toml").write_text(_manifest_text(_worker("keeper", "one")), "utf-8")
    await projects.refresh("svc")
    assert await _log_lines(services, "worker")  # still supervised while it runs

    await services.stop_service(worker)
    with pytest.raises(ActionError, match="declares no service"):
        await services.logs(LogsInput(project_id="svc", service_id="worker"))
    assert await _log_lines(services, "keeper") == []


async def test_a_service_dropped_from_the_manifest_while_stopped_leaves_at_once(
    services_env: ServicesEnv, make_project: Callable[..., Path]
) -> None:
    services, projects, discovery, _jobs, _runtime = services_env
    root = make_project("Svc", manifest=MANIFEST + _worker("worker", "one") + _worker("keeper", "one"))
    await discovery.add_project(PathInput(path=str(root)))
    (root / "forge.project.toml").write_text(_manifest_text(_worker("keeper", "one")), "utf-8")
    await projects.refresh("svc")
    with pytest.raises(ActionError, match="declares no service"):
        await services.start_service(ServiceRef(project_id="svc", service_id="worker"))


# --- a port holder the dashboard did not start is never killed without confirmation ---


async def test_start_and_restart_leave_a_listener_the_dashboard_did_not_start_and_block_for_confirmation(
    services_env: ServicesEnv, make_project: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    services, _projects, discovery, _jobs, _runtime = services_env
    port = free_port()
    pinned = f"""
[[services]]
id = "api"
label = "API"
argv = ["{PYTHON}", "scripts/listener.py", "{{port}}"]
port = {{ policy = "pinned", port = {port}, evidence = "fixture client config" }}
"""
    project = _project(make_project, pinned)
    await discovery.add_project(PathInput(path=str(project)))
    api = ServiceRef(project_id="svc", service_id="api")
    outside = await asyncio.create_subprocess_exec(
        sys.executable, "scripts/listener.py", str(port), cwd=project, stdout=asyncio.subprocess.PIPE
    )
    try:
        assert outside.stdout is not None
        await outside.stdout.readline()
        await services.scan_external(NoInput())  # the dashboard knows it as an external instance of the service

        def held_lines() -> int:
            return sum("is held by" in line for line in _lines_now(services, "api"))

        async def blocked_again(action: Callable[[ServiceRef], Awaitable[Done]], times: int) -> None:
            await action(api)
            await wait_for(lambda: held_lines() == times)
            assert outside.returncode is None

        await blocked_again(services.start_service, 1)
        await blocked_again(services.restart_service, 2)
        await wait_for(lambda: _state_of(services, "api") == "blocked")
        [blocked] = services.state.services
        assert blocked.blocked_by is not None and blocked.external is not None

        def refuse(holder: ports.PortHolder, timeout_s: float = 3.0) -> bool:
            raise PermissionError(f"cannot verify process {holder.pid} (?): access denied")

        with monkeypatch.context() as patch:
            patch.setattr(ports, "reclaim", refuse)
            with pytest.raises(ActionError, match="cannot verify process") as refused:
                await services.release_port(api)
        assert refused.value.status == 409 and outside.returncode is None

        await services.release_port(api)
        await wait_for(lambda: _state_of(services, "api") == "running")
        await asyncio.wait_for(outside.wait(), 10)
        await services.stop_service(api)
    finally:
        if outside.returncode is None:
            outside.kill()
        await outside.wait()


# --- state publishing and stopping ---


def test_a_change_reported_without_a_running_loop_fails_loudly_and_does_not_wedge_publishing(
    data_paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    projects = ProjectsService(runtime, DiscoveryService(runtime, jobs), ManifestsService(runtime))
    services = ProjectServicesService(runtime, projects, jobs)
    published: list[int] = []
    monkeypatch.setattr(services, "_publish", lambda: published.append(1))

    with pytest.raises(RuntimeError, match="no running event loop"):
        services._changed()

    async def inside_a_loop() -> None:
        services._changed()
        services._changed()  # coalesced into the same publish
        await asyncio.sleep(0)

    asyncio.run(inside_a_loop())
    assert published == [1]


async def test_stop_all_names_the_services_that_would_not_stop(
    services_env: ServicesEnv, make_project: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    services, _projects, discovery, _jobs, _runtime = services_env
    await discovery.add_project(PathInput(path=str(_project(make_project))))

    async def stuck(timeout_s: float = 20.0) -> list[str]:
        return ["backend"]

    monkeypatch.setattr(services._supervisors["svc"], "stop_all", stuck)
    with pytest.raises(ActionError, match="could not stop: svc/backend") as failure:
        await services.stop_all(NoInput())
    assert failure.value.status == 500
