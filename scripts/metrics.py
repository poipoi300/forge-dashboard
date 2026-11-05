"""The dashboard's own manifest metrics, each printing one MetricReading as JSON.

uv run python scripts/metrics.py source-lines
uv run python scripts/metrics.py bundle-size

These are this repo's metrics, declared in its forge.project.toml; they are
not part of the package or of the template other projects inherit.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_GLOBS = ("src/**/*.py", "web/src/**/*.ts", "web/src/**/*.tsx")
GENERATED = ("web/src/api/schema.d.ts",)
BUNDLE_WARN_BYTES = 2 * 1024 * 1024


def source_lines(root: Path) -> dict[str, object]:
    """Non-blank lines of hand-written Python and TypeScript."""
    generated = {root / path for path in GENERATED}
    files = [path for pattern in SOURCE_GLOBS for path in root.glob(pattern) if path not in generated]
    lines = sum(_non_blank(path.read_text(encoding="utf-8").splitlines()) for path in files)
    return {"value": lines, "detail": f"{len(files)} files"}


def _non_blank(lines: Iterable[str]) -> int:
    return sum(1 for line in lines if line.strip())


def bundle_size(root: Path) -> dict[str, object]:
    """Bytes the built WebUI ships (web/dist, source maps excluded)."""
    dist = root / "web" / "dist"
    if not dist.is_dir():
        return {"value": 0, "status": "warn", "detail": "web/dist is missing: run `bun run build` in web/"}
    size = sum(path.stat().st_size for path in dist.rglob("*") if path.is_file() and path.suffix != ".map")
    return {"value": size, "status": "warn" if size > BUNDLE_WARN_BYTES else "ok"}


METRICS = {"source-lines": source_lines, "bundle-size": bundle_size}


def main(argv: Sequence[str], root: Path = ROOT) -> int:
    if len(argv) != 1 or argv[0] not in METRICS:
        print(f"usage: metrics.py {{{','.join(METRICS)}}}", file=sys.stderr)
        return 2
    print(json.dumps(METRICS[argv[0]](root)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
