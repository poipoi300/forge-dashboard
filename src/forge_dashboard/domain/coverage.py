"""Coverage a project already reports: Cobertura XML or LCOV files at conventional paths."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from defusedxml import ElementTree
from pydantic import BaseModel, Field

from forge_dashboard.core.ui import ui

CONVENTIONAL_REPORTS = (
    "coverage.xml",
    "coverage/cobertura-coverage.xml",
    "lcov.info",
    "coverage/lcov.info",
    "web/coverage/lcov.info",
)


class CoverageReport(BaseModel):
    path: str = Field(json_schema_extra=ui(format="path"))
    format: str
    percent: float = Field(json_schema_extra=ui(format="percent"))
    lines_covered: int
    lines_total: int
    measured_at: float = Field(json_schema_extra=ui(format="relative-time"))


def parse_cobertura(text: str) -> tuple[int, int]:
    """(covered, total) lines from a Cobertura report's root attributes."""
    root = ElementTree.fromstring(text)
    covered, total = root.get("lines-covered"), root.get("lines-valid")
    if covered is not None and total is not None:
        return int(covered), int(total)
    rate = float(root.get("line-rate") or 0)
    return round(rate * 10000), 10000


def parse_lcov(text: str) -> tuple[int, int]:
    """(covered, total) lines summed over every `LH:`/`LF:` record."""
    covered = total = 0
    for line in text.splitlines():
        if line.startswith("LH:"):
            covered += int(line[3:])
        elif line.startswith("LF:"):
            total += int(line[3:])
    return covered, total


def combined_coverage(root: Path, candidates: Iterable[str] = CONVENTIONAL_REPORTS) -> CoverageReport | None:
    """Merge every conventional report present under `root` into one line-coverage figure."""
    covered = total = 0
    found: list[tuple[Path, str]] = []
    for relative in candidates:
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        kind = "cobertura" if path.suffix == ".xml" else "lcov"
        part_covered, part_total = parse_cobertura(text) if kind == "cobertura" else parse_lcov(text)
        covered += part_covered
        total += part_total
        found.append((path, kind))
    if not found or total == 0:
        return None
    newest = max(path.stat().st_mtime for path, _kind in found)
    return CoverageReport(
        path=", ".join(str(path.relative_to(root)) for path, _kind in found),
        format="+".join(dict.fromkeys(kind for _path, kind in found)),
        percent=round(100 * covered / total, 1),
        lines_covered=covered,
        lines_total=total,
        measured_at=newest,
    )
