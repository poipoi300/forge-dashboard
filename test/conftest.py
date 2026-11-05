"""Shared fixtures: isolated data dirs, runtimes, and fixture git repos (including a fake forge)."""

import os
import subprocess
import textwrap
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.services.gallery import GalleryService
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.scheduler import SchedulerService


@pytest.fixture
def data_paths(tmp_path: Path) -> DataPaths:
    return DataPaths(root=tmp_path / "data")


def _core_runtime(paths: DataPaths) -> Runtime:
    runtime = Runtime(paths)
    jobs = JobsService(runtime)
    runtime.register([jobs, SchedulerService(runtime, jobs), GalleryService(runtime, jobs)])
    return runtime


@pytest.fixture
def make_runtime(data_paths: DataPaths) -> Callable[[], Runtime]:
    """Build a fresh runtime with jobs, scheduler, and gallery registered (not started) on the same data dir."""
    return lambda: _core_runtime(data_paths)


@pytest.fixture
async def started_runtime(data_paths: DataPaths) -> AsyncIterator[Runtime]:
    runtime = _core_runtime(data_paths)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No test ever touches the real per-user data dir."""
    monkeypatch.setenv("FORGE_DASHBOARD_DATA_DIR", str(tmp_path / "default-data"))
    yield


# --- git fixture repos ---

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Fixture Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}


class Git:
    """Synchronous git for building fixture repos."""

    def __call__(self, repo: Path, *args: str, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ENV},
            check=False,
        )
        if check and completed.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed.stdout.strip()

    def init(self, repo: Path, branch: str = "main", bare: bool = False) -> Path:
        repo.mkdir(parents=True, exist_ok=True)
        self(repo, "init", "-q", "-b", branch, *(["--bare"] if bare else []))
        return repo

    def commit_all(self, repo: Path, message: str) -> str:
        self(repo, "add", "-A")
        self(repo, "commit", "-q", "-m", message)
        return self(repo, "rev-parse", "HEAD")


@pytest.fixture
def git() -> Git:
    return Git()


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8", newline="\n")


FORGE_AGENTS = """
# forge

Intro.

## What makes forge special?

Special.

## A note from the maintainer

Write structure, not quick fixes.

## A small glossary

- **gate** — one check.

## The ways to hurt yourself

1. Weakening a gate.

## Commands

| Task | Command |
|---|---|
| Tests | `uv run pytest -q` |

## Where code lives

- `src/forge/`

## Verifying

- A task is done when the gates pass.

## Commits

- Small, complete increments.

## Taste

- Guard clauses first.
"""

FORGE_PRECOMMIT = """
default_install_hook_types: [pre-commit, pre-push]
repos:
  - repo: local
    hooks:
      - id: trailing-whitespace
        name: trailing whitespace
        entry: uv run python tools/repo_hygiene.py trailing-whitespace
        language: system
        types: [text]
      - id: end-of-file-fixer
        name: end of file fixer
        entry: uv run python tools/repo_hygiene.py end-of-file-fixer
        language: system
        types: [text]
      - id: check-merge-conflict
        name: merge conflicts
        entry: uv run python tools/repo_hygiene.py check-merge-conflict
        language: system
        types: [text]
      - id: check-toml
        name: toml
        entry: uv run python tools/repo_hygiene.py check-toml
        language: system
        types: [toml]
  - repo: local
    hooks:
      - id: ruff-format
        name: ruff format
        entry: uv run ruff format --force-exclude
        language: system
        types_or: [python, pyi]
      - id: ruff-check
        name: ruff check
        entry: uv run ruff check --force-exclude --fix
        language: system
        types_or: [python, pyi]
      - id: mypy
        name: mypy
        entry: uv run mypy .
        language: system
        types_or: [python, pyi]
        pass_filenames: false
  - repo: local
    hooks:
      - id: tests-and-diff-coverage
        name: tests
        entry: bash tools/coverage_gate.sh
        language: system
        pass_filenames: false
        stages: [pre-push]
"""

FORGE_CI = """
name: CI
on:
  push:
    branches: [main]
jobs:
  lint-and-types:
    runs-on: ubuntu-latest
    steps:
      - run: uv run ruff format --check .
      - run: uv run ruff check .
      - run: uv run mypy .
  tests-and-coverage:
    runs-on: ubuntu-latest
    steps:
      - run: bash tools/coverage_gate.sh
"""

FORGE_PYPROJECT = """
[project]
name = "forge"
version = "0.1.0"
requires-python = ">=3.12"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B"]

[tool.mypy]
strict = true
"""


@pytest.fixture
def forge_repo(tmp_path: Path, git: Git) -> Path:
    """A minimal forge: the signature files, travelling AGENTS.md sections, gate configs, and tools."""
    root = tmp_path / "forge"
    write_files(
        root,
        {
            "pyproject.toml": FORGE_PYPROJECT,
            "src/forge/__init__.py": '"""forge."""\n',
            "src/forge/gates.py": '"""Gate registry."""\n',
            "AGENTS.md": FORGE_AGENTS,
            "CLAUDE.md": "@AGENTS.md\n",
            "README.md": "# forge\n\n## Setup\n\nRun it.\n",
            ".pre-commit-config.yaml": FORGE_PRECOMMIT,
            ".github/workflows/ci.yml": FORGE_CI,
            "tools/repo_hygiene.py": '"""Hygiene checks."""\nprint("hygiene")\n',
            "tools/coverage_gate.sh": "#!/usr/bin/env bash\necho coverage\n",
            ".gitignore": "__pycache__/\n.venv/\n.coverage\n",
            "forge.toml": '[runner]\ndefault_harness = "claude"\nworkspace_root = "runs"\n\n'
            '[harness.claude]\ncommand = ["claude", "-p", "{prompt}"]\n',
            "test/test_gates.py": '"""Registry mirror."""\n',
        },
    )
    git.init(root)
    git.commit_all(root, "Init forge")
    return root


MANIFEST = """
schema = 1

[project]
name = "{name}"
id = "{project_id}"
summary = "A fixture project"
kind = "app"
languages = ["python"]

[v1]
source = "milestones"

[[v1.milestones]]
id = "first"
title = "First milestone"
weight = 2
done = true

[[v1.milestones]]
id = "second"
title = "Second milestone"
"""


@pytest.fixture
def make_project(tmp_path: Path, git: Git) -> Callable[..., Path]:
    """Create a git repo with a manifest (or without one) under tmp_path/projects."""

    def make(name: str, *, manifest: str | None = MANIFEST, extra: dict[str, str] | None = None) -> Path:
        root = tmp_path / "projects" / name
        files = dict(extra or {})
        if manifest is not None:
            files["forge.project.toml"] = manifest.replace("{name}", name).replace("{project_id}", name.lower())
        files.setdefault("README.md", f"# {name}\n")
        write_files(root, files)
        git.init(root)
        git.commit_all(root, f"Init {name}")
        return root

    return make
