"""The suite stays in its own repositories when a git hook runs it."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fixture_repositories_ignore_the_repository_a_hook_exports(tmp_path: Path) -> None:
    outer = tmp_path / "outer.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outer)], check=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "test/test_git.py::test_real_git_log_and_status_round_trip",
        ],
        cwd=ROOT,
        env={**os.environ, "GIT_DIR": str(outer)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    refs = subprocess.run(["git", "--git-dir", str(outer), "for-each-ref"], capture_output=True, text=True, check=True)
    assert refs.stdout == ""
