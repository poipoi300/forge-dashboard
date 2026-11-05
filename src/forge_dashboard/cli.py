"""forge-dashboard command-line interface.

uv run forge-dashboard manifest validate path/to/repo
uv run forge-dashboard manifest schema > schema/forge.project.schema.json
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from forge_dashboard.manifest import load_manifest
from forge_dashboard.manifest.schema import manifest_json_schema_text


def _manifest_validate(target: Path) -> int:
    result = load_manifest(target)
    if result.valid:
        print(f"{result.path}: ok")
        return 0
    for issue in result.issues:
        print(issue.render(result.path))
    return 1


def _manifest_schema() -> int:
    sys.stdout.write(manifest_json_schema_text())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge-dashboard", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest", help="validate manifests or print the manifest JSON Schema")
    manifest_commands = manifest.add_subparsers(dest="manifest_command", required=True)
    validate = manifest_commands.add_parser("validate", help="validate a forge.project.toml (file or repo dir)")
    validate.add_argument("path", type=Path)
    manifest_commands.add_parser("schema", help="print the forge.project.toml JSON Schema")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "manifest":
        if args.manifest_command == "validate":
            return _manifest_validate(args.path)
        return _manifest_schema()
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
