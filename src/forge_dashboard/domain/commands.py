"""Turn manifest argv/env templates into concrete commands with the contract's environment."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from forge_dashboard.manifest import placeholders as ph


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


def contract_env(project_root: Path, project_id: str, dashboard_url: str | None) -> dict[str, str]:
    """The variables every command the dashboard starts receives."""
    return {
        "FORGE_PROJECT_ROOT": str(project_root),
        "FORGE_PROJECT_ID": project_id,
        "FORGE_DASHBOARD_URL": dashboard_url or "",
    }


def render_command(
    *,
    argv: Iterable[str],
    cwd: str,
    env: Mapping[str, str],
    project_root: Path,
    project_id: str,
    dashboard_url: str | None,
    values: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> Command:
    """Substitute placeholders and assemble the environment (contract vars, then extras, then the manifest's env).

    Args:
        cwd: The manifest's repo-relative cwd.
        values: Placeholder values beyond `{project_root}` (`port`, `host`, `report_path`).
    """
    substitutions = {ph.PROJECT_ROOT: str(project_root), **(values or {})}
    rendered_env = {key: ph.render(value, substitutions) for key, value in env.items()}
    return Command(
        argv=tuple(ph.render(part, substitutions) for part in argv),
        cwd=project_root / cwd,
        env={**contract_env(project_root, project_id, dashboard_url), **(extra_env or {}), **rendered_env},
    )
