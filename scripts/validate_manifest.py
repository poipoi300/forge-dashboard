"""The "Validate a manifest" custom action: params arrive as JSON on stdin.

stdin: {"path": "<a forge.project.toml, or a repo directory>"}
"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from forge_dashboard.cli import main as cli_main


def main(stdin: TextIO) -> int:
    params = json.load(stdin)
    path = params.get("path")
    if not isinstance(path, str) or not path:
        print("params need a non-empty `path`", file=sys.stderr)
        return 2
    return cli_main(["manifest", "validate", path])


if __name__ == "__main__":
    sys.exit(main(sys.stdin))
