"""`list`, `status`, `check`, `sync`, and `openapi`: in-process, against a served dashboard, and as pure formatting."""

import asyncio
import io
import json
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import respx
import uvicorn
from conftest import MANIFEST, Git
from test_forks import build_fork

from forge_dashboard import cli
from forge_dashboard.cli import (
    Cell,
    CliError,
    HttpBackend,
    InProcessBackend,
    ago,
    color_enabled,
    exit_code_for,
    main,
    openapi_text,
    paint,
    render_projects,
    render_status,
    render_table,
    running_server_url,
    safe_text,
    strip_ansi,
)
from forge_dashboard.core.db import Database
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.domain.summary import (
    AttentionItem,
    CheckFacts,
    CiFacts,
    ForkFacts,
    GitFacts,
    Health,
    HealthLevel,
    HealthReason,
    IssueFacts,
    LastChange,
    Lifecycle,
    ProjectSummary,
    ServiceBrief,
    StandardsFacts,
    V1Progress,
)
from forge_dashboard.server import build_app
from forge_dashboard.services.discovery import DiscoveryService
from forge_dashboard.services.jobs import JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService

PYTHON = sys.executable.replace(chr(92), "/")
PASSING = f"""
[[checks]]
id = "lint"
label = "Lint"
argv = ["{PYTHON}", "-c", "print('lint ok')"]
stage = "commit"
"""
FAILING = f"""
[[checks]]
id = "gate"
label = "Gate"
argv = ["{PYTHON}", "-c", "import sys; print('gate broken'); sys.exit(3)"]
stage = "commit"
"""


def seed_discovery(paths: DataPaths, root: Path) -> None:
    """Point discovery at `root` before the dashboard first starts, so it scans nothing else."""

    async def seed() -> None:
        database = await Database.open(paths.database)
        try:
            await database.kv_put("config", "discovery", {"roots": [str(root)], "max_depth": 3})
        finally:
            await database.close()

    asyncio.run(seed())


@pytest.fixture
def dashboard(tmp_path: Path, make_project: Callable[..., Path]) -> DataPaths:
    """Three fixture projects found by a dashboard whose data lives in the autouse temporary data dir."""
    make_project("Alpha", manifest=MANIFEST + PASSING)
    make_project("Broken", manifest=MANIFEST + FAILING)
    make_project("Bare")
    paths = DataPaths.default()
    seed_discovery(paths, tmp_path / "projects")
    return paths


# --- in-process ---


def test_list_prints_one_row_per_project_under_a_header(
    dashboard: DataPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["ID", "NAME", "STATUS", "HEALTH", "CHANGED", "V1", "CHECKS", "GIT"]
    rows = {line.split()[0]: line.split() for line in lines[1:]}
    assert sorted(rows) == ["alpha", "bare", "broken"]
    assert rows["alpha"][1:3] == ["Alpha", "active"] and "67%" in rows["alpha"]
    assert "\x1b" not in "\n".join(lines)


def test_status_prints_the_projects_facts(dashboard: DataPaths, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "alpha"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "alpha: Alpha"
    facts = {line.split()[0]: line.split(None, 1)[1] for line in out[1:]}
    assert facts["role"] == "managed" and facts["kind"] == "app; python" and facts["status"] == "active"
    assert facts["v1"].startswith("67%") and facts["git"].startswith("main")


def test_status_of_an_unknown_project_is_a_usage_error(
    dashboard: DataPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status", "nope"]) == 2

    captured = capsys.readouterr()
    assert captured.out == "" and "error: no project 'nope'" in captured.err


def test_check_streams_the_log_and_exits_zero_when_the_checks_pass(
    dashboard: DataPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "alpha"]) == 0
    assert main(["check", "alpha", "--check", "lint"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out.count("lint ok") == 2
    assert out[-1] == "check alpha: succeeded (1 passed)"
    assert "── Lint (lint)" in out


def test_check_exits_one_when_a_check_fails(dashboard: DataPaths, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "broken"]) == 1

    out = capsys.readouterr().out.splitlines()
    assert "gate broken" in out and out[-1] == "check broken: failed (failing: Gate)"


def test_check_exits_two_for_unknown_projects_and_checks_and_one_without_checks(
    dashboard: DataPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "ghost"]) == 2
    assert main(["check", "alpha", "--check", "nope"]) == 2
    assert main(["check", "bare"]) == 1

    err = capsys.readouterr().err.splitlines()
    assert err == [
        "error: no project 'ghost'",
        "error: no check 'nope' in the manifest",
        "error: this project's manifest declares no checks",
    ]


def test_sync_lands_a_clean_upstream_change_and_reports_success(
    dashboard: DataPaths, tmp_path: Path, git: Git, capsys: pytest.CaptureFixture[str]
) -> None:
    repos = build_fork(tmp_path, git, conflict=False)

    assert main(["sync", "forked"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("sync forked: succeeded")
    assert (repos.checkout / "upstream-only.txt").is_file()


def test_sync_of_a_project_that_is_not_a_fork_fails(dashboard: DataPaths, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sync", "alpha"]) == 1

    assert capsys.readouterr().out.splitlines()[-1].startswith("sync alpha: failed")


def test_a_stale_server_file_falls_back_to_running_in_process(
    dashboard: DataPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    dashboard.root.mkdir(parents=True, exist_ok=True)
    dashboard.server_file.write_text(
        json.dumps({"url": "http://127.0.0.1:1", "pid": 1, "started_at": 0}), encoding="utf-8"
    )

    assert main(["list"]) == 0
    assert "alpha" in capsys.readouterr().out


def test_an_unreadable_server_file_is_reported(dashboard: DataPaths, capsys: pytest.CaptureFixture[str]) -> None:
    dashboard.server_file.write_text("not json", encoding="utf-8")

    assert main(["list"]) == 1
    assert "is not a valid server description; delete it" in capsys.readouterr().err


# --- against a running server ---


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def serving(paths: DataPaths, expected_projects: int) -> Iterator[str]:
    """The dashboard API on a free port in a background thread, once it lists `expected_projects` projects."""
    url = f"http://127.0.0.1:{_free_port()}"
    _runtime, app = build_app(paths, None, url)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=int(url.rsplit(":", 1)[1]), log_level="warning", ws="none")
    )
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), name="test-dashboard", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                listed = httpx.get(f"{url}/api/projects", timeout=2).json()
                if httpx.get(f"{url}/api/health", timeout=2).is_success and len(listed) == expected_projects:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "the test dashboard never listed its projects"
            time.sleep(0.1)
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=60)


def refuse_in_process_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(_paths: DataPaths) -> Runtime:
        raise AssertionError("the CLI must use the running server, not build its own runtime")

    monkeypatch.setattr("forge_dashboard.container.build_runtime", forbidden)


def test_commands_talk_to_the_running_server_found_through_server_json(
    dashboard: DataPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with serving(dashboard, expected_projects=3):
        refuse_in_process_runtime(monkeypatch)

        assert main(["list"]) == 0
        assert sorted(line.split()[0] for line in capsys.readouterr().out.splitlines()[1:]) == [
            "alpha",
            "bare",
            "broken",
        ]

        assert main(["status", "alpha"]) == 0
        assert capsys.readouterr().out.splitlines()[0] == "alpha: Alpha"
        assert main(["status", "nope"]) == 2
        assert "no project 'nope'" in capsys.readouterr().err

        assert main(["check", "alpha"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert "lint ok" in out and out[-1] == "check alpha: succeeded (1 passed)"

        assert main(["check", "broken"]) == 1
        assert capsys.readouterr().out.splitlines()[-1] == "check broken: failed (failing: Gate)"
        assert main(["check", "ghost"]) == 2
        assert main(["check", "alpha", "--check", "nope"]) == 2
        assert main(["check", "bare"]) == 1
        assert capsys.readouterr().err.splitlines() == [
            "error: no project 'ghost'",
            "error: no check 'nope' in the manifest",
            "error: this project's manifest declares no checks",
        ]


def test_sync_runs_through_the_running_server(
    dashboard: DataPaths, tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repos = build_fork(tmp_path, git, conflict=False)
    with serving(dashboard, expected_projects=4):
        refuse_in_process_runtime(monkeypatch)

        assert main(["sync", "forked"]) == 0

    assert capsys.readouterr().out.splitlines()[-1].startswith("sync forked: succeeded")
    assert (repos.checkout / "upstream-only.txt").is_file()


async def test_the_server_file_is_only_trusted_while_the_server_answers(tmp_path: Path) -> None:
    paths = DataPaths(root=tmp_path / "data")
    assert await running_server_url(paths) is None
    paths.ensure()
    paths.server_file.write_text(json.dumps({"url": "http://127.0.0.1:1", "pid": 1, "started_at": 0}), encoding="utf-8")
    assert await running_server_url(paths) is None


@pytest.mark.respx(base_url="http://dashboard.test")
async def test_the_http_backend_polls_the_log_until_the_job_is_done(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "POLL_INTERVAL_S", 0.001)
    respx_mock.post("/api/services/forks/actions/sync").respond(
        json={"job_id": "j1", "kind": "forks.sync", "title": "t"}
    )
    chunk = {"job_id": "j1", "offset": 0, "next_offset": 0, "lines": [], "done": False}
    log = respx_mock.post("/api/services/jobs/actions/log")
    log.side_effect = [
        httpx.Response(200, json=chunk),
        httpx.Response(200, json={**chunk, "next_offset": 2, "lines": ["a", "b"]}),
        httpx.Response(200, json={**chunk, "offset": 2, "next_offset": 3, "lines": ["c"], "done": True}),
        httpx.Response(200, json={**chunk, "offset": 3, "next_offset": 3, "done": True}),
    ]
    job = {
        "id": "j1",
        "kind": "forks.sync",
        "title": "t",
        "status": "attention",
        "created_at": 1.0,
        "summary": "parked",
    }
    respx_mock.post("/api/services/jobs/actions/get").respond(json={"job": job, "result": None})
    seen: list[str] = []

    async with httpx.AsyncClient(base_url="http://dashboard.test") as client:
        final = await HttpBackend(client).run_job("forks", "sync", {"project_id": "x"}, seen.append)

    assert seen == ["a", "b", "c"] and final.status is JobStatus.ATTENTION
    offsets = [json.loads(call.request.content)["offset"] for call in log.calls]
    assert offsets == [0, 0, 2, 3]


@pytest.mark.respx(base_url="http://dashboard.test")
async def test_http_failures_carry_the_servers_detail_and_a_matching_exit_code(respx_mock: respx.MockRouter) -> None:
    respx_mock.post("/api/services/checks/actions/run").mock(
        side_effect=[
            httpx.Response(404, json={"detail": "no project 'x'"}),
            httpx.Response(409, json={"detail": "no checks"}),
            httpx.Response(422, json={"detail": [{"msg": "bad"}]}),
            httpx.Response(500, text="boom"),
            httpx.Response(502, json=["gateway"]),
        ]
    )
    async with httpx.AsyncClient(base_url="http://dashboard.test") as client:
        backend = HttpBackend(client)
        failures: list[CliError] = []
        for _ in range(5):
            with pytest.raises(CliError) as failure:
                await backend.run_job("checks", "run", {}, print)
            failures.append(failure.value)

    assert [(str(failure), failure.exit_code) for failure in failures] == [
        ("no project 'x'", 2),
        ("no checks", 1),
        ('[{"msg": "bad"}]', 2),
        ("boom", 1),
        ('["gateway"]', 1),
    ]


async def test_waiting_for_discovery_hints_once_and_gives_up(
    data_paths: DataPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    projects = ProjectsService(runtime, DiscoveryService(runtime, jobs), ManifestsService(runtime))
    runtime.register([jobs, projects])
    monkeypatch.setattr(cli, "LOAD_HINT_AFTER_S", 0.05)
    monkeypatch.setattr(cli, "LOAD_TIMEOUT_S", 0.3)

    with pytest.raises(CliError, match="did not finish within") as failure:
        await InProcessBackend(runtime).wait_until_loaded()

    assert failure.value.exit_code == 1
    assert capsys.readouterr().err == "waiting for project discovery...\n"


# --- openapi ---


def test_openapi_prints_the_document_without_starting_anything(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["openapi"]) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["info"]["title"] == "forge dashboard"
    assert "/api/services/checks/actions/run" in document["paths"]
    assert "/api/projects" in document["paths"]
    assert not DataPaths.default().root.exists(), "the document is built over a throwaway data dir"


# --- formatting ---


def test_manifest_commands_still_work_through_the_same_parser(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["manifest", "schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "forge.project.toml"


def test_a_missing_subcommand_or_project_is_an_argparse_usage_error() -> None:
    for argv in ([], ["status"], ["check"], ["sync"], ["nonsense"]):
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == 2


@pytest.mark.parametrize(("status", "code"), [(404, 2), (422, 2), (409, 1), (500, 1), (400, 1)])
def test_exit_codes_follow_the_failure_kind(status: int, code: int) -> None:
    assert exit_code_for(status) == code


def test_tables_align_columns_and_keep_alignment_when_coloured() -> None:
    rows = [[Cell("a"), Cell("failing", "bad")], [Cell("longer"), Cell("ok")]]

    assert render_table(["ID", "STATE"], rows, color=False) == ["ID      STATE", "a       failing", "longer  ok"]
    coloured = render_table(["ID", "STATE"], rows, color=True)
    assert coloured[1] == "a       \x1b[31mfailing\x1b[0m"
    assert [strip_ansi(line) for line in coloured] == render_table(["ID", "STATE"], rows, color=False)


def test_colour_only_for_a_terminal_without_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert color_enabled(Terminal()) and not color_enabled(io.StringIO())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not color_enabled(Terminal())
    assert paint("x", "good", color=True) == "\x1b[32mx\x1b[0m"
    assert paint("x", "good", color=False) == paint("x", None, color=True) == "x"


def test_log_lines_lose_terminal_escapes_and_unencodable_characters() -> None:
    assert strip_ansi("\x1b[1;32mPassed\x1b[0m \x1b[2K done") == "Passed  done"
    console = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    assert safe_text("ok ✓ café", console) == "ok ? caf?"


def test_relative_times() -> None:
    now = 1_000_000.0
    assert [
        ago(None, now),
        ago(now - 5, now),
        ago(now - 120, now),
        ago(now - 7200, now),
        ago(now - 3 * 86400, now),
    ] == [
        "never",
        "just now",
        "2m ago",
        "2h ago",
        "3d ago",
    ]


def _summary(**overrides: object) -> ProjectSummary:
    fields: dict[str, object] = {
        "id": "alpha",
        "name": "Alpha",
        "role": "managed",
        "path": "/p/alpha",
        "machine": "box",
    }
    return ProjectSummary.model_validate({**fields, **overrides})


def test_a_bare_project_renders_dashes_where_facts_are_missing() -> None:
    [header, row] = render_projects([_summary()], now=1_000.0, color=False)

    assert header.split() == ["ID", "NAME", "STATUS", "HEALTH", "CHANGED", "V1", "CHECKS", "GIT"]
    assert row.split() == ["alpha", "Alpha", "active", "unknown", "never", "-", "-", "-"]


def test_a_full_project_renders_every_signal_in_list_and_status() -> None:
    now = 1_000_000.0
    summary = _summary(
        status=Lifecycle.BLOCKED,
        status_note="waiting on upstream",
        kind="app",
        languages=["python", "rust"],
        health=Health(
            level=HealthLevel.FAILING,
            reasons=[HealthReason(source="ci", level=HealthLevel.FAILING, message="CI is failing on main")],
        ),
        last_change=LastChange(at=now - 7200, source="commit"),
        v1=V1Progress(percent=62.6, caption="5/8 milestones"),
        checks=CheckFacts(passed=1, failed=1, total=2, ran_at=now - 120, stale=True),
        ci=CiFacts(conclusion="failure", workflows=3),
        issues=IssueFacts(open_issues=4, open_prs=None),
        git=GitFacts(branch="main", dirty=3),
        fork=ForkFacts(is_fork=True, upstream="owner/repo", last_sync="conflict"),
        standards=StandardsFacts(passing=12, total=13),
        services=[
            ServiceBrief(id="api", label="API", state="running", address="127.0.0.1:8760", machine="box"),
            ServiceBrief(id="worker", label="Worker", state="stopped", machine="box"),
        ],
        attention=[
            AttentionItem(source="ci", level="bad", message="CI failing"),
            AttentionItem(source="git", level="warn", message="3 dirty"),
        ],
    )

    [_, row] = render_projects([summary], now=now, color=False)
    assert row.split(None, 3)[:3] == ["alpha", "Alpha", "blocked"]
    assert row.split()[3:] == ["failing", "2h", "ago", "63%", "1/2", "stale", "main", "(3", "dirty)"]

    status = render_status(summary, now=now, color=False)
    assert status[0] == "alpha: Alpha"
    body = "\n".join(status[1:])
    for expected in (
        "role       managed",
        "kind       app; python, rust",
        "status     blocked (waiting on upstream)",
        "health     failing",
        "changed    2h ago (commit)",
        "v1         63% 5/8 milestones",
        "checks     1/2 passing, ran 2m ago (stale)",
        "ci         failure, 3 workflows",
        "issues     4 open, 0 PRs",
        "git        main (3 dirty)",
        "fork       upstream owner/repo, last sync conflict",
        "standards  12/13 passing",
        "agent      0 sessions",
        "service    API: running at 127.0.0.1:8760",
        "service    Worker: stopped",
        "because    [ci] CI is failing on main",
        "attention  [ci] CI failing",
        "attention  [git] 3 dirty",
    ):
        assert expected in body
    coloured = render_status(summary, now=now, color=True)
    assert any("\x1b[31mfailing\x1b[0m" in line for line in coloured)
    assert any("\x1b[33m[git] 3 dirty\x1b[0m" in line for line in coloured)


def test_openapi_output_writes_the_document_with_lf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"

    assert main(["openapi", "--output", str(target)]) == 0

    raw = target.read_bytes()
    assert b"\r\n" not in raw
    assert json.loads(raw)["info"]["title"] == "forge dashboard"


def test_the_committed_web_openapi_document_matches_the_api() -> None:
    committed = Path(__file__).resolve().parents[1] / "web" / "openapi.json"

    assert json.loads(committed.read_text(encoding="utf-8")) == json.loads(openapi_text()), (
        "web/openapi.json is stale: run `bun run gen:api` in web/"
    )
