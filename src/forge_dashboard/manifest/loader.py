"""Read `forge.project.toml` into a `ProjectManifest`, or into located issues.

An invalid manifest never raises out of `load_manifest`: the dashboard shows a
project with a broken manifest together with its exact errors, so the result
always carries either a manifest or the issues that prevented one.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from forge_dashboard.manifest.locator import concrete_path, key_lines, line_for
from forge_dashboard.manifest.models import MANIFEST_FILENAME, ManifestRuleViolation, ProjectManifest

_TOML_POSITION = re.compile(r"\(at line (?P<line>\d+), column (?P<column>\d+)\)")
_VALUE_ERROR_PREFIX = "Value error, "


class ManifestIssue(BaseModel):
    """One problem with a manifest, pointing at the line that causes it.

    Attributes:
        location: Dotted key path such as `checks[1].argv`; empty for
            document-level problems.
        line: 1-based line in the file, when one applies.
        message: What is wrong.
    """

    location: str
    line: int | None
    message: str

    def render(self, path: Path | str) -> str:
        """Format as `path:line: location: message` for terminals and logs."""
        position = f"{path}:{self.line}" if self.line is not None else f"{path}"
        where = f" {self.location}:" if self.location else ""
        return f"{position}:{where} {self.message}"


class ManifestLoad(BaseModel):
    """The outcome of reading one manifest file."""

    path: str
    manifest: ProjectManifest | None
    issues: list[ManifestIssue]

    @property
    def valid(self) -> bool:
        return self.manifest is not None


def format_location(path: Iterable[str | int]) -> str:
    """Render `("checks", 1, "argv")` as `checks[1].argv`."""
    rendered = ""
    for segment in path:
        if isinstance(segment, int):
            rendered += f"[{segment}]"
        else:
            rendered += f".{segment}" if rendered else segment
    return rendered


def _issues_from_validation(
    error: ValidationError, data: Mapping[str, Any], lines: Mapping[tuple[str | int, ...], int]
) -> list[ManifestIssue]:
    issues: list[ManifestIssue] = []
    for detail in error.errors():
        base_loc = tuple(detail["loc"])
        cause = (detail.get("ctx") or {}).get("error")
        located: list[tuple[tuple[str | int, ...], str]]
        if isinstance(cause, ManifestRuleViolation):
            located = [((*base_loc, *loc), message) for loc, message in cause.problems]
        else:
            located = [(base_loc, detail["msg"].removeprefix(_VALUE_ERROR_PREFIX))]
        for loc, message in located:
            path = concrete_path(data, loc)
            issues.append(ManifestIssue(location=format_location(path), line=line_for(lines, path), message=message))
    return sorted(issues, key=lambda issue: (issue.line or 0, issue.location))


def parse_manifest_text(text: str, path: str = MANIFEST_FILENAME) -> ManifestLoad:
    """Validate manifest source text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        position = _TOML_POSITION.search(str(error))
        line = int(position["line"]) if position else None
        message = _TOML_POSITION.sub("", str(error)).strip()
        return ManifestLoad(path=path, manifest=None, issues=[ManifestIssue(location="", line=line, message=message)])
    try:
        manifest = ProjectManifest.model_validate(data)
    except ValidationError as error:
        return ManifestLoad(path=path, manifest=None, issues=_issues_from_validation(error, data, key_lines(text)))
    return ManifestLoad(path=path, manifest=manifest, issues=[])


def manifest_path(target: Path) -> Path:
    """Accept either a manifest file or the directory that holds one."""
    return target / MANIFEST_FILENAME if target.is_dir() else target


def load_manifest(target: Path) -> ManifestLoad:
    """Read and validate a manifest file, or the manifest inside a directory."""
    path = manifest_path(target)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ManifestLoad(
            path=str(path), manifest=None, issues=[ManifestIssue(location="", line=None, message="file not found")]
        )
    except UnicodeDecodeError as error:
        return ManifestLoad(
            path=str(path),
            manifest=None,
            issues=[ManifestIssue(location="", line=None, message=f"not UTF-8: {error.reason}")],
        )
    return parse_manifest_text(text, str(path))
