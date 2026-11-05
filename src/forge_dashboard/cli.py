"""forge-dashboard command-line interface.

uv run forge-dashboard list
uv run forge-dashboard status <project>
uv run forge-dashboard check <project> [--check ID]
uv run forge-dashboard sync <project>
uv run forge-dashboard openapi --output web/openapi.json
uv run forge-dashboard manifest validate path/to/repo
uv run forge-dashboard manifest schema > schema/forge.project.schema.json

`list`, `status`, `check`, and `sync` talk to the running dashboard server when
one answers at the address in its `server.json`, and otherwise run the same
services in-process. Exit codes: 0 success, 1 failure, 2 usage error or
unknown project.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TextIO

import httpx
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.service import ActionError, JobRef
from forge_dashboard.domain.summary import (
    CheckFacts,
    GitFacts,
    HealthLevel,
    ProjectSummary,
    V1Progress,
)
from forge_dashboard.manifest import load_manifest
from forge_dashboard.manifest.schema import manifest_json_schema_text
from forge_dashboard.services.jobs import (
    JobDetail,
    JobLogChunk,
    JobLogInput,
    JobsService,
    JobStatus,
    JobSummary,
)
from forge_dashboard.services.projects import ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
# What an action's 404 (unknown project, check, or job) or 422 (invalid input) means for the user: a usage error.
USAGE_STATUSES = frozenset({404, 422})

HEALTH_PROBE_TIMEOUT_S = 2.0
REQUEST_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 0.25
LOAD_TIMEOUT_S = 300.0
LOAD_HINT_AFTER_S = 2.0

Tone = Literal["good", "warn", "bad"]
_ANSI_TONES: dict[Tone, str] = {"good": "32", "warn": "33", "bad": "31"}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_HEALTH_LABELS = {
    HealthLevel.GOOD: "healthy",
    HealthLevel.WARN: "warn",
    HealthLevel.FAILING: "failing",
    HealthLevel.UNKNOWN: "unknown",
}
_HEALTH_TONES: dict[HealthLevel, Tone | None] = {
    HealthLevel.GOOD: "good",
    HealthLevel.WARN: "warn",
    HealthLevel.FAILING: "bad",
    HealthLevel.UNKNOWN: None,
}


class CliError(Exception):
    """A failure to report on stderr and turn into an exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def exit_code_for(status: int) -> int:
    """The exit code for an action that failed with HTTP-style `status`."""
    return EXIT_USAGE if status in USAGE_STATUSES else EXIT_FAILED


# --- text output ---


def color_enabled(stream: TextIO) -> bool:
    """Colour only for a terminal, and never when the user asked for none (NO_COLOR)."""
    return stream.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, tone: Tone | None, *, color: bool) -> str:
    if not color or tone is None:
        return text
    return f"\x1b[{_ANSI_TONES[tone]}m{text}\x1b[0m"


def strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def safe_text(text: str, stream: TextIO) -> str:
    """`text` as `stream` can encode it: a job log may hold characters a console code page lacks."""
    encoding = stream.encoding or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding)


def ago(timestamp: float | None, now: float) -> str:
    if timestamp is None:
        return "never"
    seconds = max(0, int(now - timestamp))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


@dataclass(frozen=True)
class Cell:
    text: str
    tone: Tone | None = None


def render_table(headers: Sequence[str], rows: Sequence[Sequence[Cell]], *, color: bool) -> list[str]:
    """Aligned columns separated by two spaces; padding is added before colour so alignment survives it."""
    widths = [max([len(header), *(len(row[index].text) for row in rows)]) for index, header in enumerate(headers)]
    lines = ["  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)).rstrip()]
    for row in rows:
        cells = [
            paint(cell.text, cell.tone, color=color) + " " * (width - len(cell.text))
            for cell, width in zip(row, widths, strict=True)
        ]
        lines.append("  ".join(cells).rstrip())
    return lines


def _v1_text(v1: V1Progress | None) -> str:
    return f"{v1.percent:.0f}%" if v1 is not None else "-"


def _checks_cell(checks: CheckFacts | None) -> Cell:
    if checks is None or not checks.total:
        return Cell("-")
    text = f"{checks.passed}/{checks.total}" + (" stale" if checks.stale else "")
    return Cell(text, "bad" if checks.failed else None)


def _git_text(git: GitFacts | None) -> str:
    if git is None or git.branch is None:
        return "-"
    return f"{git.branch} ({git.dirty} dirty)" if git.dirty else git.branch


def _health_cell(summary: ProjectSummary) -> Cell:
    level = summary.health.level
    return Cell(_HEALTH_LABELS[level], _HEALTH_TONES[level])


def render_projects(projects: Sequence[ProjectSummary], now: float, *, color: bool) -> list[str]:
    rows = [
        [
            Cell(summary.id),
            Cell(summary.name),
            Cell(summary.status.value),
            _health_cell(summary),
            Cell(ago(summary.last_change.at, now)),
            Cell(_v1_text(summary.v1)),
            _checks_cell(summary.checks),
            Cell(_git_text(summary.git)),
        ]
        for summary in projects
    ]
    return render_table(["ID", "NAME", "STATUS", "HEALTH", "CHANGED", "V1", "CHECKS", "GIT"], rows, color=color)


def render_status(summary: ProjectSummary, now: float, *, color: bool) -> list[str]:
    """Every fact the overview holds for one project, one labelled line each."""
    facts: list[tuple[str, Cell]] = [
        ("path", Cell(summary.path)),
        ("role", Cell(summary.role)),
        ("kind", Cell(f"{summary.kind or '-'}; {', '.join(summary.languages) or 'no languages'}")),
        ("status", Cell(summary.status.value + (f" ({summary.status_note})" if summary.status_note else ""))),
        ("health", _health_cell(summary)),
        ("changed", Cell(f"{ago(summary.last_change.at, now)} ({summary.last_change.source})")),
        ("v1", Cell(f"{_v1_text(summary.v1)} {summary.v1.caption}".strip() if summary.v1 is not None else "-")),
    ]
    checks = summary.checks
    if checks is not None and checks.total:
        ran = f", ran {ago(checks.ran_at, now)}" if checks.ran_at else ""
        facts.append(
            ("checks", Cell(f"{checks.passed}/{checks.total} passing{ran}{' (stale)' if checks.stale else ''}"))
        )
    if summary.ci is not None:
        facts.append(("ci", Cell(f"{summary.ci.conclusion}, {summary.ci.workflows} workflows")))
    if summary.issues is not None and summary.issues.open_issues is not None:
        facts.append(("issues", Cell(f"{summary.issues.open_issues} open, {summary.issues.open_prs or 0} PRs")))
    facts.append(("git", Cell(_git_text(summary.git))))
    if summary.fork is not None and summary.fork.is_fork:
        facts.append(("fork", Cell(f"upstream {summary.fork.upstream}, last sync {summary.fork.last_sync}")))
    if summary.standards is not None:
        facts.append(("standards", Cell(f"{summary.standards.passing}/{summary.standards.total} passing")))
    facts.append(("agent", Cell("active" if summary.agent.active else f"{summary.agent.session_count} sessions")))
    facts.extend(
        ("service", Cell(f"{service.label}: {service.state}" + (f" at {service.address}" if service.address else "")))
        for service in summary.services
    )
    facts.extend(("because", Cell(f"[{reason.source}] {reason.message}")) for reason in summary.health.reasons)
    facts.extend(("attention", Cell(f"[{item.source}] {item.message}", item.level)) for item in summary.attention)
    return [
        f"{summary.id}: {summary.name}",
        *(f"  {label:<10} {paint(cell.text, cell.tone, color=color)}" for label, cell in facts),
    ]


# --- talking to the dashboard ---


class Backend(Protocol):
    """Where a command's data and jobs come from: the running server, or services run in this process."""

    async def projects(self) -> list[ProjectSummary]: ...

    async def run_job(
        self, service: str, action: str, payload: dict[str, JsonValue], emit: Callable[[str], None]
    ) -> JobSummary:
        """Start the long-running action, pass its log lines to `emit`, and return the finished job."""
        ...


class ServerInfo(BaseModel):
    """What a running server writes to `server.json`."""

    url: str
    pid: int
    started_at: float


async def running_server_url(paths: DataPaths) -> str | None:
    """The base URL of the dashboard server that wrote `server.json`, when it still answers its health check.

    Raises:
        CliError: when `server.json` exists but is not a server description.
    """
    if not paths.server_file.is_file():
        return None
    try:
        info = ServerInfo.model_validate_json(paths.server_file.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise CliError(
            f"{paths.server_file} is not a valid server description; delete it ({error.errors()[0]['msg']})"
        ) from error
    try:
        async with httpx.AsyncClient(timeout=HEALTH_PROBE_TIMEOUT_S) as client:
            response = await client.get(f"{info.url}/api/health")
    except httpx.HTTPError:
        return None  # the file outlived its server; run in-process instead
    return info.url if response.status_code == httpx.codes.OK else None


def _failure_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else json.dumps(detail if detail is not None else body)


class HttpBackend:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _send(self, request: Awaitable[httpx.Response]) -> Any:
        response = await request
        if response.is_error:
            raise CliError(_failure_detail(response), exit_code_for(response.status_code))
        return response.json()

    async def _post(self, service: str, action: str, payload: dict[str, JsonValue]) -> Any:
        return await self._send(self._client.post(f"/api/services/{service}/actions/{action}", json=payload))

    async def projects(self) -> list[ProjectSummary]:
        return TypeAdapter(list[ProjectSummary]).validate_python(await self._send(self._client.get("/api/projects")))

    async def run_job(
        self, service: str, action: str, payload: dict[str, JsonValue], emit: Callable[[str], None]
    ) -> JobSummary:
        job = JobRef.model_validate(await self._post(service, action, payload))
        offset = 0
        while True:
            request = JobLogInput(job_id=job.job_id, offset=offset).model_dump(mode="json")
            chunk = JobLogChunk.model_validate(await self._post("jobs", "log", request))
            for line in chunk.lines:
                emit(line)
            offset = chunk.next_offset
            if chunk.done and not chunk.lines:
                break
            if not chunk.lines:
                await asyncio.sleep(POLL_INTERVAL_S)
        detail = JobDetail.model_validate(await self._post("jobs", "get", {"job_id": job.job_id}))
        return detail.job


class InProcessBackend:
    def __init__(self, runtime: Runtime) -> None:
        projects, jobs = runtime.services["projects"], runtime.services["jobs"]
        assert isinstance(projects, ProjectsService) and isinstance(jobs, JobsService)
        self._runtime = runtime
        self._projects = projects
        self._jobs = jobs

    async def wait_until_loaded(self) -> None:
        """Wait for discovery to publish the project list (a first scan of a large home directory takes a while)."""
        started = time.monotonic()
        hinted = False
        while not self._projects.state.loaded:
            elapsed = time.monotonic() - started
            if elapsed > LOAD_TIMEOUT_S:
                raise CliError(f"project discovery did not finish within {LOAD_TIMEOUT_S:.0f}s; start the dashboard")
            if elapsed > LOAD_HINT_AFTER_S and not hinted:
                print("waiting for project discovery...", file=sys.stderr)
                hinted = True
            await asyncio.sleep(0.1)

    async def projects(self) -> list[ProjectSummary]:
        return list(self._projects.state.projects)

    async def run_job(
        self, service: str, action: str, payload: dict[str, JsonValue], emit: Callable[[str], None]
    ) -> JobSummary:
        try:
            started = await self._runtime.invoke(service, action, payload)
        except ActionError as error:
            raise CliError(error.message, exit_code_for(error.status)) from error
        job = JobRef.model_validate(started.model_dump())
        summary = await self._jobs.wait(job.job_id)
        offset = 0
        while True:
            chunk = await self._jobs.log(JobLogInput(job_id=job.job_id, offset=offset))
            for line in chunk.lines:
                emit(line)
            if not chunk.lines:
                return summary
            offset = chunk.next_offset


@asynccontextmanager
async def open_backend(paths: DataPaths) -> AsyncIterator[Backend]:
    """The running server's API when one answers, else a runtime started (and stopped) here."""
    url = await running_server_url(paths)
    if url is not None:
        async with httpx.AsyncClient(base_url=url, timeout=REQUEST_TIMEOUT_S) as client:
            yield HttpBackend(client)
        return
    # Imported here because building the services costs a few hundred milliseconds the manifest commands never need.
    from forge_dashboard.container import build_runtime

    runtime = build_runtime(paths)
    await runtime.start()
    try:
        backend = InProcessBackend(runtime)
        await backend.wait_until_loaded()
        yield backend
    finally:
        await runtime.stop()


# --- commands ---

Handler = Callable[[argparse.Namespace, Backend], Awaitable[int]]


async def _list(_args: argparse.Namespace, backend: Backend) -> int:
    projects = await backend.projects()
    if not projects:
        print("no projects found")
        return EXIT_OK
    color = color_enabled(sys.stdout)
    print("\n".join(render_projects(projects, time.time(), color=color)))
    return EXIT_OK


async def _status(args: argparse.Namespace, backend: Backend) -> int:
    summary = next((project for project in await backend.projects() if project.id == args.project), None)
    if summary is None:
        raise CliError(f"no project {args.project!r}; `forge-dashboard list` shows the known ids", EXIT_USAGE)
    print("\n".join(render_status(summary, time.time(), color=color_enabled(sys.stdout))))
    return EXIT_OK


async def _run_job(backend: Backend, service: str, action: str, payload: dict[str, JsonValue], subject: str) -> int:
    color = color_enabled(sys.stdout)

    def emit(line: str) -> None:
        print(safe_text(line if color else strip_ansi(line), sys.stdout))

    job = await backend.run_job(service, action, payload, emit)
    succeeded = job.status is JobStatus.SUCCEEDED
    detail = job.summary or job.error
    verdict = f"{subject}: {job.status.value}" + (f" ({detail})" if detail else "")
    print(safe_text(paint(verdict, "good" if succeeded else "bad", color=color), sys.stdout))
    return EXIT_OK if succeeded else EXIT_FAILED


async def _check(args: argparse.Namespace, backend: Backend) -> int:
    payload: dict[str, JsonValue] = {"project_id": args.project, "check_id": args.check_id}
    return await _run_job(backend, "checks", "run", payload, f"check {args.project}")


async def _sync(args: argparse.Namespace, backend: Backend) -> int:
    return await _run_job(backend, "forks", "sync", {"project_id": args.project}, f"sync {args.project}")


async def _with_backend(handler: Handler, args: argparse.Namespace) -> int:
    async with open_backend(DataPaths.default()) as backend:
        return await handler(args, backend)


def openapi_text() -> str:
    """The API's OpenAPI document as JSON; the app is built, not started, over a throwaway data dir."""
    from forge_dashboard.server import build_app

    with tempfile.TemporaryDirectory(prefix="forge-dashboard-openapi-") as scratch:
        _runtime, app = build_app(DataPaths(root=Path(scratch)), None, "http://127.0.0.1:0")
        document = app.openapi()
    return json.dumps(document, indent=2) + "\n"


def descriptors_text() -> str:
    """Every service's descriptor as served by `GET /api/services`, for the web test fixtures."""
    from pydantic import TypeAdapter

    from forge_dashboard.container import build_runtime
    from forge_dashboard.core.descriptors import ServiceDescriptor, describe

    with tempfile.TemporaryDirectory(prefix="forge-dashboard-descriptors-") as scratch:
        runtime = build_runtime(DataPaths(root=Path(scratch)))
        descriptors = [describe(service) for service in runtime.services.values()]
    payload = TypeAdapter(list[ServiceDescriptor]).dump_python(descriptors, mode="json", by_alias=True)
    return json.dumps(payload, indent=2) + "\n"


def _write_or_print(text: str, output: Path | None) -> int:
    if output is None:
        sys.stdout.write(text)
    else:
        output.write_text(text, encoding="utf-8", newline="\n")
    return EXIT_OK


def _manifest_validate(target: Path) -> int:
    result = load_manifest(target)
    if result.valid:
        print(f"{result.path}: ok")
        return 0
    for issue in result.issues:
        print(issue.render(result.path))
    return 1


def _manifest_schema() -> int:
    sys.stdout.write(manifest_json_schema_text())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge-dashboard", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="list every project with its health").set_defaults(handler=_list)
    status = commands.add_parser("status", help="show everything the overview knows about one project")
    status.add_argument("project", help="project id (see `list`)")
    status.set_defaults(handler=_status)
    check = commands.add_parser("check", help="run a project's manifest checks and stream their output")
    check.add_argument("project", help="project id (see `list`)")
    check.add_argument("--check", dest="check_id", help="run only this check id")
    check.set_defaults(handler=_check)
    sync = commands.add_parser("sync", help="sync a fork with its upstream and stream the output")
    sync.add_argument("project", help="project id (see `list`)")
    sync.set_defaults(handler=_sync)
    openapi = commands.add_parser("openapi", help="print the API's OpenAPI document as JSON")
    openapi.add_argument("--output", type=Path, help="write it to this file (LF line endings) instead of stdout")
    descriptors = commands.add_parser("descriptors", help="print every service descriptor as JSON")
    descriptors.add_argument("--output", type=Path, help="write it to this file (LF line endings) instead of stdout")

    manifest = commands.add_parser("manifest", help="validate manifests or print the manifest JSON Schema")
    manifest_commands = manifest.add_subparsers(dest="manifest_command", required=True)
    validate = manifest_commands.add_parser("validate", help="validate a forge.project.toml (file or repo dir)")
    validate.add_argument("path", type=Path)
    manifest_commands.add_parser("schema", help="print the forge.project.toml JSON Schema")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "manifest":
        if args.manifest_command == "validate":
            return _manifest_validate(args.path)
        return _manifest_schema()
    if args.command == "openapi":
        return _write_or_print(openapi_text(), args.output)
    if args.command == "descriptors":
        return _write_or_print(descriptors_text(), args.output)
    try:
        return asyncio.run(_with_backend(args.handler, args))
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
