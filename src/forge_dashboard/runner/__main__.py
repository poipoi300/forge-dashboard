"""forge dashboard runner: supervise the dashboard's services and open the WebUI.

uv run -m forge_dashboard.runner            # TUI; opens the browser when the API is ready
uv run -m forge_dashboard.runner --dev      # adds the Bun dev server with HMR
uv run -m forge_dashboard.runner --check    # preflight: executables and ports, then exit

Without a TTY the runner streams plain, prefixed logs instead of drawing the TUI.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import webbrowser
from collections.abc import Callable, Sequence

from forge_dashboard.domain.requirements import DoctorError, read_report
from forge_dashboard.process import ports
from forge_dashboard.runner.specs import REPO_ROOT, RESERVED_PORTS, browser_target, dashboard_specs
from forge_dashboard.runner.tui import RunnerApp
from forge_dashboard.supervisor.core import ServiceSpec, Supervisor, SupervisorEvent, SupervisorEventKind

LOG_DIR = REPO_ROOT / "logs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge_dashboard.runner", description=__doc__)
    parser.add_argument("--dev", action="store_true", help="also run the Bun dev server with HMR")
    parser.add_argument("--no-autostart", action="store_true", help="open the TUI without starting services")
    parser.add_argument("--no-browser", action="store_true", help="do not open the WebUI when ready")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="manage a subset of services")
    parser.add_argument("--check", action="store_true", help="preflight executables and ports, then exit")
    parser.add_argument("--plain", action="store_true", help="stream plain logs even on a TTY")
    return parser


def select(specs: list[ServiceSpec], only: Sequence[str] | None) -> list[ServiceSpec]:
    if not only:
        return specs
    unknown = sorted(set(only) - {spec.name for spec in specs})
    if unknown:
        raise SystemExit(f"unknown services: {', '.join(unknown)} (available: {', '.join(s.name for s in specs)})")
    return [spec for spec in specs if spec.name in only]


def preflight(specs: list[ServiceSpec]) -> list[str]:
    """Port problems that would stop a service from starting."""
    problems: list[str] = []
    for spec in specs:
        request = spec.port
        if request is not None and request.policy == "pinned" and request.pinned is not None:
            problems.extend(
                f"{spec.name}: port {request.pinned} is held by {holder.name} (pid {holder.pid})"
                for holder in ports.holders([request.pinned])
            )
    reserved_conflicts = [
        f"{spec.name}: preferred port {spec.port.preferred} belongs to other software"
        for spec in specs
        if spec.port is not None and spec.port.preferred in RESERVED_PORTS
    ]
    return problems + reserved_conflicts


def _tool_problems() -> list[str]:
    try:
        report = read_report(REPO_ROOT, versions=True)
    except DoctorError as error:
        return [str(error)]
    for tool in report.tools:
        owner = "package ownership unknown" if tool.ownership_unknown else tool.package
        details = ", ".join(value for value in (tool.version, tool.path, owner) if value)
        line = f"{tool.name}: {tool.status}" + (f" — {details}" if details else "")
        print("".join(character if character.isprintable() else ascii(character)[1:-1] for character in line))
    for name, harness in report.harnesses.items():
        print(f"harness {name}: {harness.status}")
    return [f"required tool '{name}' is missing" for name in report.missing_required]


def _opener(enabled: bool) -> Callable[[str], object]:
    def open_url(url: str) -> object:
        if enabled:
            return webbrowser.open(url)
        return None

    return open_url


async def run_plain(specs: list[ServiceSpec], *, open_target: str | None, open_url: Callable[[str], object]) -> int:
    """Supervise without a TUI: prefixed log lines on stdout until interrupted."""
    stop = asyncio.Event()
    opened = False
    supervisor: Supervisor

    def on_line(service: str, line: str) -> None:
        print(f"[{service}] {line}", flush=True)

    def on_event(event: SupervisorEvent) -> None:
        nonlocal opened
        print(f"[runner] {event.service}: {event.kind.value} {event.detail}".rstrip(), flush=True)
        if event.kind is SupervisorEventKind.READY and event.service == open_target and not opened:
            opened = True
            url = supervisor.status(event.service).url
            if url is not None:
                print(f"[runner] open {url}", flush=True)
                open_url(url)

    supervisor = Supervisor(specs, log_tag="runner", on_line=on_line, on_event=on_event, reserved_ports=RESERVED_PORTS)
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(signum, stop.set)
    await supervisor.start_all()
    try:
        await stop.wait()
    finally:
        print("[runner] stopping services", flush=True)
        await supervisor.stop_all()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    specs = select(dashboard_specs(dev=args.dev), args.only)
    if args.check:
        problems = _tool_problems() + preflight(specs)
        for problem in problems:
            print(f"problem: {problem}")
        print("ok" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 1
    target = browser_target(args.dev)
    open_target = target if any(spec.name == target for spec in specs) else None
    open_url = _opener(not args.no_browser)
    if args.plain or not sys.stdout.isatty():
        try:
            return asyncio.run(run_plain(specs, open_target=open_target, open_url=open_url))
        except KeyboardInterrupt:
            return 0
    app: RunnerApp
    supervisor = Supervisor(
        specs, log_tag="runner", on_change=lambda: app.supervisor_changed(), reserved_ports=RESERVED_PORTS
    )
    app = RunnerApp(
        supervisor,
        log_dir=LOG_DIR,
        autostart=not args.no_autostart,
        open_when_ready=open_target,
        open_url=open_url,
    )
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
