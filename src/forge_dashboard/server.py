"""Run the API (and the built UI) under uvicorn.

uv run -m forge_dashboard.server --port 8760

The runner starts this as its `api` service. It runs uvicorn inside our own
event loop rather than through `uvicorn.run`, so the loop is the platform
default (Proactor on Windows), which is the only one that supports the
subprocesses every job needs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from forge_dashboard.api.app import create_app
from forge_dashboard.api.routes import ROUTE_INSTALLERS
from forge_dashboard.container import build_runtime
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATIC = REPO_ROOT / "web" / "dist"


def build_app(paths: DataPaths, static_dir: Path | None, url: str) -> tuple[Runtime, FastAPI]:
    runtime = build_runtime(paths)
    runtime.dashboard_url = url

    async def on_ready(ready_runtime: Runtime) -> None:
        payload = {"url": url, "pid": os.getpid(), "started_at": time.time()}
        ready_runtime.paths.server_file.write_text(json.dumps(payload), encoding="utf-8")

    app = create_app(runtime, static_dir=static_dir, route_installers=ROUTE_INSTALLERS, on_ready=on_ready)
    return runtime, app


async def serve(host: str, port: int, static_dir: Path | None) -> None:
    paths = DataPaths.default()
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    _runtime, app = build_app(paths, static_dir, url)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", ws="websockets", lifespan="on")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        with contextlib.suppress(FileNotFoundError):
            paths.server_file.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge_dashboard.server", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--static", type=Path, default=DEFAULT_STATIC, help="built UI directory")
    args = parser.parse_args(argv)
    asyncio.run(serve(args.host, args.port, args.static if args.static.is_dir() else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
