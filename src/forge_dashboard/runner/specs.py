"""The dashboard's own services, as the runner supervises them.

- `api`: uvicorn serving the API and the built UI, on a dynamic port preferring 8760,
  after `bun install` and `bun run build` (each skipped when a content-hash stamp says
  nothing changed).
- `web` (only with `--dev`): Bun's dev server with HMR, proxying `/api` to `api`.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from forge_dashboard.supervisor.core import Endpoint, LaunchPlan, PortRequest, PreStep, Readiness, ServiceSpec

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB = REPO_ROOT / "web"
DIST = WEB / "dist"
API_PREFERRED_PORT = 8760
WEB_PREFERRED_PORT = 8761
RESERVED_PORTS = (8000, 8317)

INSTALL_INPUTS = ("package.json", "bun.lock")
BUILD_INPUTS = ("package.json", "bun.lock", "build.ts", "tsconfig.json", "bunfig.toml", "src")


def content_hash(root: Path, inputs: Iterable[str]) -> str:
    """SHA-256 over the relative paths and bytes of every file under `inputs`, in sorted order."""
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in inputs:
        target = root / name
        if target.is_dir():
            files.extend(path for path in target.rglob("*") if path.is_file())
        elif target.is_file():
            files.append(target)
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class Stamp:
    """A pre-step is current when its stamp file holds the hash of its inputs."""

    root: Path
    inputs: tuple[str, ...]
    stamp_file: Path

    def is_current(self) -> bool:
        return self.stamp_file.is_file() and self.stamp_file.read_text(encoding="utf-8") == content_hash(
            self.root, self.inputs
        )

    def mark(self) -> None:
        self.stamp_file.parent.mkdir(parents=True, exist_ok=True)
        self.stamp_file.write_text(content_hash(self.root, self.inputs), encoding="utf-8")


def install_stamp(web: Path = WEB) -> Stamp:
    return Stamp(web, INSTALL_INPUTS, web / "node_modules" / ".forge-install-stamp")


def build_stamp(web: Path = WEB) -> Stamp:
    return Stamp(web, BUILD_INPUTS, web / "dist" / ".forge-build-stamp")


def _api_plan(dev: bool) -> ServiceSpec:
    def plan(endpoint: Endpoint, _providers: Mapping[str, Endpoint]) -> LaunchPlan:
        argv = [sys.executable, "-m", "forge_dashboard.server", "--host", endpoint.host, "--port", str(endpoint.port)]
        argv += ["--no-static"] if dev else ["--static", str(DIST)]
        return LaunchPlan(argv=tuple(argv), cwd=REPO_ROOT, env={})

    install = install_stamp()
    build = build_stamp()
    pre_steps = [
        PreStep("bun install", ("bun", "install"), WEB, is_current=install.is_current, mark_current=install.mark)
    ]
    if not dev:
        pre_steps.append(
            PreStep("bun run build", ("bun", "run", "build"), WEB, is_current=build.is_current, mark_current=build.mark)
        )
    return ServiceSpec(
        name="api",
        description="API and built UI (uvicorn)",
        host="127.0.0.1",
        port=PortRequest(policy="dynamic", preferred=API_PREFERRED_PORT),
        readiness=Readiness(kind="http", path="/api/health"),
        owner_root=REPO_ROOT,
        plan=plan,
        pre_steps=tuple(pre_steps),
        open_path="/",
    )


def _web_plan() -> ServiceSpec:
    def plan(endpoint: Endpoint, providers: Mapping[str, Endpoint]) -> LaunchPlan:
        api = providers["api"]
        env = {"PORT": str(endpoint.port), "HOST": endpoint.host, "API_URL": api.base_url or ""}
        return LaunchPlan(argv=("bun", "run", "dev"), cwd=WEB, env=env)

    return ServiceSpec(
        name="web",
        description="Bun dev server with HMR, proxying /api",
        host="127.0.0.1",
        port=PortRequest(policy="dynamic", preferred=WEB_PREFERRED_PORT),
        readiness=Readiness(kind="http", path="/"),
        owner_root=REPO_ROOT,
        plan=plan,
        depends_on=("api",),
        open_path="/",
    )


def dashboard_specs(*, dev: bool) -> list[ServiceSpec]:
    """`api`, plus `web` in dev mode."""
    specs = [_api_plan(dev)]
    if dev:
        specs.append(_web_plan())
    return specs


def browser_target(dev: bool) -> str:
    """Which service's URL the runner opens once it is ready."""
    return "web" if dev else "api"
