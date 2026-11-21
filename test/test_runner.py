"""The TUI runner: specs and stamps, preflight, the plain-log path, and the Textual app driven headless."""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forge_dashboard.domain.requirements import DoctorError, DoctorReport, HarnessReport, ToolReport
from forge_dashboard.runner import __main__ as runner_main
from forge_dashboard.runner.specs import (
    API_PREFERRED_PORT,
    REPO_ROOT,
    Stamp,
    browser_target,
    content_hash,
    dashboard_specs,
)
from forge_dashboard.runner.tui import COLUMNS, HelpScreen, RunnerApp, detail_line, format_duration, row_cells
from forge_dashboard.supervisor.core import (
    Endpoint,
    LaunchPlan,
    PortRequest,
    Readiness,
    ServiceSpec,
    ServiceStatus,
    Supervisor,
)
from forge_dashboard.supervisor.policy import ServiceState

PY = sys.executable


def test_production_runs_only_the_api_with_install_and_build_pre_steps() -> None:
    [api] = dashboard_specs(dev=False)
    assert api.name == "api" and api.port is not None and api.port.preferred == API_PREFERRED_PORT
    assert [step.label for step in api.pre_steps] == ["bun install", "bun run build"]
    plan = api.plan(Endpoint("127.0.0.1", 8760), {})
    assert plan.argv[1:3] == ("-m", "forge_dashboard.server") and "--static" in plan.argv
    assert browser_target(False) == "api"


def test_dev_adds_the_bun_dev_server_proxied_to_the_api() -> None:
    api, web = dashboard_specs(dev=True)
    assert [step.label for step in api.pre_steps] == ["bun install"]
    assert "--no-static" in api.plan(Endpoint("127.0.0.1", 8760), {}).argv
    plan = web.plan(Endpoint("127.0.0.1", 8761), {"api": Endpoint("127.0.0.1", 8760)})
    assert plan.argv == ("bun", "run", "dev") and plan.env["API_URL"] == "http://127.0.0.1:8760"
    assert web.depends_on == ("api",) and browser_target(True) == "web"


def test_content_stamps_skip_unchanged_pre_steps(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("a", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    stamp = Stamp(tmp_path, ("package.json", "src"), tmp_path / "dist" / ".stamp")
    assert not stamp.is_current()
    stamp.mark()
    assert stamp.is_current()
    first = content_hash(tmp_path, ("package.json", "src"))
    (tmp_path / "src" / "a.ts").write_text("b", encoding="utf-8")
    assert not stamp.is_current() and content_hash(tmp_path, ("package.json", "src")) != first


def test_only_selects_services_and_rejects_unknown_names() -> None:
    specs = dashboard_specs(dev=True)
    assert [s.name for s in runner_main.select(specs, ["web"])] == ["web"]
    assert runner_main.select(specs, None) == specs
    with pytest.raises(SystemExit, match="unknown services: nope"):
        runner_main.select(specs, ["nope"])


def test_check_reports_ok_and_exits_zero(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    bun = ToolReport(
        name="bun",
        status="unmanaged",
        required=True,
        path="/local/bun",
        version="1.4",
        package=None,
        ownership_unknown=False,
    )
    pi = HarnessReport(cli="pi", status="unavailable", missing=("pi",))
    report = DoctorReport(ok=True, tools=(bun,), harnesses={"pi": pi})

    def read_report(manifest: Path, *, versions: bool) -> DoctorReport:
        assert manifest == REPO_ROOT and versions
        return report

    monkeypatch.setattr(runner_main, "read_report", read_report)
    assert runner_main.main(["--check"]) == 0
    output = capsys.readouterr().out
    assert "bun: unmanaged — 1.4" in output
    assert "harness pi: unavailable" in output
    assert output.strip().endswith("ok")
    report = DoctorReport(
        ok=False, tools=(bun.model_copy(update={"status": "missing", "version": None}),), harnesses={}
    )
    assert runner_main.main(["--check"]) == 1
    assert "required tool 'bun' is missing" in capsys.readouterr().out


def test_check_names_unavailable_doctor(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(manifest: Path, *, versions: bool) -> DoctorReport:
        raise DoctorError("Forge doctor is unavailable")

    monkeypatch.setattr(runner_main, "read_report", missing)
    assert runner_main.main(["--check"]) == 1
    assert "problem: Forge doctor is unavailable" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="the launcher requires Bash")
def test_bash_check_forwards_preflight_failure_without_installing_tools(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    dirname = shutil.which("dirname")
    assert bash is not None and dirname is not None
    (tmp_path / "bash").symlink_to(bash)
    (tmp_path / "dirname").symlink_to(dirname)
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\ntest "$1" = --directory && test -f "$2/run.sh" || exit 22\n'
        'shift 2\ntest "$*" = "run --no-sync -m forge_dashboard.runner --check" || exit 22\n'
        'printf "problem: required tool bun is missing\n"\nexit 17\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    result = subprocess.run(
        (str(REPO_ROOT / "run.sh"), "--check"),
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 17
    assert result.stdout.strip() == "problem: required tool bun is missing"

    uv.unlink()
    result = subprocess.run(
        (str(REPO_ROOT / "run.sh"), "--check"),
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "uv is required to run dashboard preflight."


def _status(state: ServiceState, **fields: object) -> ServiceStatus:
    base: dict[str, object] = {
        "name": "api",
        "description": "API",
        "state": state,
        "detail": "",
        "pid": None,
        "host": "127.0.0.1",
        "port": None,
        "url": None,
        "started_at": None,
        "ready_at": None,
        "retry_at": None,
        "restarts": 0,
        "autorestart": True,
        "blocked_by": None,
    }
    base.update(fields)
    return ServiceStatus.model_validate(base)


def test_rows_and_detail_are_pure_functions_of_the_snapshot_and_tick() -> None:
    running = _status(ServiceState.RUNNING, pid=42, port=8760, ready_at=100.0, restarts=2, url="http://127.0.0.1:8760/")
    cells = row_cells(running, 100.0 + 3725, COLUMNS)
    assert [str(cell) for cell in cells] == ["api", "running", "42", "8760", "1h02m", "2"]
    assert detail_line(running, 0) == "API · http://127.0.0.1:8760/"
    backoff = _status(ServiceState.BACKOFF, retry_at=110.0, detail="exited with code 1; retrying")
    assert detail_line(backoff, 100.0) == "API · retry in 10s · exited with code 1; retrying"
    assert [str(c) for c in row_cells(backoff, 100.0, ("service", "uptime"))] == ["api", ""]
    assert (format_duration(None), format_duration(59), format_duration(61)) == ("", "59s", "1m01s")


def _sleeper(name: str, tmp_path: Path, *, port: bool = False) -> ServiceSpec:
    script = "import time; print('hello from " + name + "', flush=True); time.sleep(60)"

    def plan(_endpoint: Endpoint, _providers: object) -> LaunchPlan:
        return LaunchPlan(argv=(PY, "-c", script), cwd=tmp_path, env={})

    return ServiceSpec(
        name=name,
        description=f"{name} for tests",
        host="127.0.0.1",
        port=PortRequest(policy="dynamic") if port else None,
        readiness=Readiness(kind="none"),
        owner_root=tmp_path,
        plan=plan,
        open_path="/",
    )


async def test_the_tui_drives_the_supervisor_from_the_keyboard(tmp_path: Path) -> None:
    opened: list[str] = []
    app: RunnerApp
    supervisor = Supervisor(
        [_sleeper("api", tmp_path, port=True), _sleeper("worker", tmp_path)],
        log_tag="runner",
        on_change=lambda: app.supervisor_changed(),
    )
    app = RunnerApp(supervisor, log_dir=tmp_path / "logs", open_when_ready="api", open_url=opened.append)
    async with app.run_test(size=(140, 32)) as pilot:
        for _ in range(100):
            await pilot.pause(0.05)
            if all(s.state is ServiceState.RUNNING for s in supervisor.snapshot()):
                break
        table = app.query_one("#services")
        assert [str(table.get_cell_at((0, 0))), str(table.get_cell_at((0, 1)))] == ["api", "running"]  # type: ignore[attr-defined]
        assert opened and opened[0].startswith("http://127.0.0.1:")
        await pilot.press("j")
        await pilot.press("x")
        for _ in range(100):
            await pilot.pause(0.05)
            if supervisor.status("worker").state is ServiceState.STOPPED:
                break
        assert supervisor.status("worker").state is ServiceState.STOPPED
        await pilot.press("l")
        assert (tmp_path / "logs" / "worker.log").read_text(encoding="utf-8").count("hello from worker") == 1
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        app.save_screenshot("runner.svg", str(tmp_path))
        await pilot.press("q")
        await asyncio.sleep(0)
    assert {s.state for s in supervisor.snapshot()} == {ServiceState.STOPPED}
    assert (tmp_path / "runner.svg").stat().st_size > 1000


async def test_narrow_terminals_drop_pid_and_stack_the_log(tmp_path: Path) -> None:
    app: RunnerApp
    supervisor = Supervisor([_sleeper("api", tmp_path)], on_change=lambda: app.supervisor_changed())
    app = RunnerApp(supervisor, log_dir=tmp_path, autostart=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        table = app.query_one("#services")
        assert "pid" not in [str(column.label) for column in table.columns.values()]  # type: ignore[attr-defined]
        assert app.query_one("#body").has_class("stacked")
        await pilot.press("q")


async def test_plain_mode_streams_prefixed_lines_and_opens_the_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    task = asyncio.create_task(
        runner_main.run_plain([_sleeper("api", tmp_path, port=True)], open_target="api", open_url=opened.append)
    )
    # Readiness "none" makes the service ready at spawn, before the child prints; wait for both.
    out = ""
    for _ in range(200):
        await asyncio.sleep(0.05)
        out += capsys.readouterr().out
        if opened and "[api] hello from api" in out:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    out += capsys.readouterr().out
    assert "[api] hello from api" in out and "[runner] api: ready" in out
    assert opened and opened[0].startswith("http://127.0.0.1:")
