"""The shared host-tool report, obtained from the Forge doctor CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from forge_dashboard.manifest.models import Slug, ToolName

FORGE_REPO_ENV = "FORGE_DASHBOARD_FORGE_REPO"
_RUN_CHECKOUT = (
    "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('forge.cli', run_name='__main__')"
)


class DoctorError(Exception):
    """Forge doctor could not provide a report."""


class ToolReport(BaseModel):
    name: ToolName
    status: Literal["missing", "present", "unmanaged"]
    required: bool
    path: str | None
    version: str | None
    package: str | None
    ownership_unknown: bool


class HarnessReport(BaseModel):
    cli: ToolName
    status: Literal["available", "unavailable"]
    missing: tuple[ToolName, ...]


class DoctorReport(BaseModel):
    ok: bool
    tools: tuple[ToolReport, ...]
    harnesses: dict[Slug, HarnessReport]

    @property
    def missing_required(self) -> list[str]:
        return [tool.name for tool in self.tools if tool.required and tool.status == "missing"]


def _doctor_command() -> tuple[str, ...]:
    checkout = os.environ.get(FORGE_REPO_ENV)
    if checkout:
        source = Path(checkout).resolve() / "src"
        if not (source / "forge" / "doctor.py").is_file():
            raise DoctorError(f"{FORGE_REPO_ENV} must point to a checkout with Forge doctor")
        return (sys.executable, "-I", "-B", "-c", _RUN_CHECKOUT, str(source))
    # The discovery signature is descriptive. It does not authorize code execution.
    for entry in os.get_exec_path():
        directory = Path(entry)
        if directory.is_absolute():
            installed = shutil.which(str(directory / "forge"))
            if installed is not None:
                return (installed,)
    raise DoctorError(f"Forge doctor is unavailable. Set {FORGE_REPO_ENV} to a trusted Forge checkout")


def read_report(manifest: Path, *, versions: bool = False) -> DoctorReport:
    """Read Forge's report. Only the runner's own trusted manifest opts into version probes."""
    argv = (*_doctor_command(), "doctor", "--json", "--manifest", str(manifest.absolute()))
    if versions:
        argv = (*argv, "--trust-manifest")
    try:
        completed = subprocess.run(
            argv,
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            # Each of the dashboard manifest's five tools can take two 15-second probes.
            timeout=180 if versions else 60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DoctorError("Forge doctor could not run") from error
    if completed.returncode not in (0, 1):
        raise DoctorError("Forge doctor could not check the manifest")
    try:
        report = DoctorReport.model_validate_json(completed.stdout)
    except ValidationError as error:
        raise DoctorError("Forge doctor returned an invalid report") from error
    if report.ok != (completed.returncode == 0) or report.ok == bool(report.missing_required):
        raise DoctorError("Forge doctor returned an inconsistent report")
    return report
