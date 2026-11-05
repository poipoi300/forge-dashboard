"""Launch and interface instructions for the project page.

The manifest's `[launch].instructions` wins: inline markdown, or a
repo-relative path to a `.md` file. Without it the guide is the README's
first setup or run section, the same section the standards check looks for.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from forge_dashboard.core.ui import ui
from forge_dashboard.manifest import ProjectManifest

README_RUN_HEADING = re.compile(
    r"^(?P<hashes>#{1,3}) .*\b(setup|install|installation|run|running|usage|getting started|quick ?start|launch)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_HEADING = re.compile(r"^(?P<hashes>#{1,6}) ", re.MULTILINE)
_FENCE = re.compile(r"^(```|~~~)", re.MULTILINE)


class LaunchGuide(BaseModel):
    source: Literal["manifest", "manifest-file", "readme", "none"] = Field(
        json_schema_extra=ui(
            format="badge",
            enum_labels={
                "manifest": "from the manifest",
                "manifest-file": "from the manifest's file",
                "readme": "from the README",
                "none": "no instructions",
            },
        )
    )
    markdown: str = Field(json_schema_extra=ui(format="markdown"))
    path: str | None = Field(None, json_schema_extra=ui(format="path"))
    primary_service: str | None = Field(None, json_schema_extra=ui(format="code"))
    problem: str | None = None


def _outside_fences(markdown: str, position: int) -> bool:
    return len(_FENCE.findall(markdown, 0, position)) % 2 == 0


def readme_run_section(readme: str) -> str | None:
    """The first setup/run heading outside code fences, up to the next heading of the same or higher level."""
    start = next(
        (match for match in README_RUN_HEADING.finditer(readme) if _outside_fences(readme, match.start())), None
    )
    if start is None:
        return None
    level = len(start.group("hashes"))
    end = len(readme)
    for heading in _HEADING.finditer(readme, start.end()):
        if len(heading.group("hashes")) <= level and _outside_fences(readme, heading.start()):
            end = heading.start()
            break
    return readme[start.start() : end].rstrip() + "\n"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        return None


def _manifest_guide(root: Path, instructions: str, primary: str | None) -> LaunchGuide:
    looks_like_file = instructions.strip().lower().endswith(".md") and "\n" not in instructions.strip()
    if not looks_like_file:
        return LaunchGuide(source="manifest", markdown=instructions, primary_service=primary)
    target = (root / instructions.strip()).resolve()
    if not target.is_relative_to(root.resolve()):
        return LaunchGuide(
            source="manifest-file",
            markdown="",
            primary_service=primary,
            problem=f"[launch].instructions points outside the project: {instructions.strip()}",
        )
    text = _read_text(target)
    if text is None:
        return LaunchGuide(
            source="manifest-file",
            markdown="",
            path=str(target),
            primary_service=primary,
            problem=f"[launch].instructions names {instructions.strip()}, which is missing or not UTF-8 text",
        )
    return LaunchGuide(source="manifest-file", markdown=text, path=str(target), primary_service=primary)


def launch_guide(root: Path, manifest: ProjectManifest | None) -> LaunchGuide:
    """Instructions for launching and using the project, with where they came from."""
    launch = manifest.launch if manifest is not None else None
    primary = launch.primary_service if launch is not None else None
    if primary is None and manifest is not None and manifest.services:
        primary = manifest.services[0].id
    if launch is not None and launch.instructions:
        return _manifest_guide(root, launch.instructions, primary)
    readme_path = root / "README.md"
    readme = _read_text(readme_path)
    section = readme_run_section(readme) if readme is not None else None
    if section is None:
        return LaunchGuide(
            source="none",
            markdown="",
            primary_service=primary,
            problem="no README.md" if readme is None else "the README has no setup or run section",
        )
    return LaunchGuide(source="readme", markdown=section, path=str(readme_path), primary_service=primary)
