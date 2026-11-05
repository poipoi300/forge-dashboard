"""Manifest services: launch with dependencies and ports, external instances, crashes, and deployments."""

import asyncio
import socket
import sys
from collections.abc import AsyncIterator, Callable
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
from forge_dashboard.core.service import ActionError, NoInput, ProjectRef
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
