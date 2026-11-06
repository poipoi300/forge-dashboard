"""Run the API (and the built UI) under uvicorn.

uv run -m forge_dashboard.server --port 8760

The runner starts this as its `api` service. It runs uvicorn inside our own
event loop rather than through `uvicorn.run`, so the loop is the platform
default (Proactor on Windows), which is the only one that supports the
subprocesses every job needs.

The API runs code on this machine, so it listens on loopback and answers only
to loopback `Host` and `Origin` values. To reach it from another computer, bind
a non-loopback `--host` and name every host name clients will type with
`--allow-host`:

uv run -m forge_dashboard.server --port 8760 --host 0.0.0.0 --allow-host dashboard.lan

Anyone who can reach that address controls this machine; put it behind a
network you trust.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from forge_dashboard.api.access import LOOPBACK_HOSTS, is_loopback_host, parse_host_name
from forge_dashboard.api.app import create_app
from forge_dashboard.api.routes import ROUTE_INSTALLERS
from forge_dashboard.container import build_runtime
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATIC = REPO_ROOT / "web" / "dist"
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})


def advertised_host(host: str) -> str:
    """The name a client on this computer uses to reach a server bound to `host`, in URL form."""
    if host in WILDCARD_HOSTS:
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host


def build_app(
    paths: DataPaths, static_dir: Path | None, url: str, allowed_hosts: Iterable[str] = LOOPBACK_HOSTS
) -> tuple[Runtime, FastAPI]:
    runtime = build_runtime(paths)
    runtime.dashboard_url = url

    async def on_ready(ready_runtime: Runtime) -> None:
        ready_runtime.paths.server_file.write_text(json.dumps({"url": url}), encoding="utf-8")

    app = create_app(
        runtime,
        static_dir=static_dir,
        route_installers=ROUTE_INSTALLERS,
        on_ready=on_ready,
        allowed_hosts=allowed_hosts,
    )
    return runtime, app


def server_endpoint(host: str, port: int, allow_hosts: Iterable[str]) -> tuple[str, frozenset[str]]:
    """The URL written to `server.json` and the host names the API serves: loopback, `allow_hosts`, and that URL's."""
    url_host = advertised_host(host)
    return f"http://{url_host}:{port}", frozenset({*LOOPBACK_HOSTS, *allow_hosts, parse_host_name(url_host)})


async def serve(host: str, port: int, static_dir: Path | None, allow_hosts: Iterable[str] = ()) -> None:
    paths = DataPaths.default()
    url, allowed_hosts = server_endpoint(host, port, allow_hosts)
    _runtime, app = build_app(paths, static_dir, url, allowed_hosts)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", ws="websockets", lifespan="on")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        with contextlib.suppress(FileNotFoundError):
            paths.server_file.unlink()


def _host_name(text: str) -> str:
    try:
        return parse_host_name(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge_dashboard.server", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="address to bind; anything but loopback needs --allow-host")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        type=_host_name,
        metavar="NAME",
        help="host name or IP address clients reach the API by (repeatable); loopback names are always served",
    )
    parser.add_argument("--static", type=Path, default=DEFAULT_STATIC, help="built UI directory")
    parser.add_argument("--no-static", action="store_true", help="serve only the API (the dev server serves the UI)")
    args = parser.parse_args(argv)
    if not is_loopback_host(args.host) and not args.allow_host:
        parser.error(
            f"--host {args.host!r} exposes the API, which runs code on this machine, beyond this computer; "
            "name every host name clients will use with --allow-host to confirm"
        )
    static = None if args.no_static or not args.static.is_dir() else args.static
    asyncio.run(serve(args.host, args.port, static, args.allow_host))
    return 0


if __name__ == "__main__":
    sys.exit(main())
