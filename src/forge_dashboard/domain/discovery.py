"""What counts as a project, decided from a directory's own files.

- `forge.project.toml` present: a managed project.
- The forge repo itself (pyproject name `forge` plus `src/forge/gates.py`): the
  standards source.
- An AGENTS.md derived from forge's plus forge gate configs, but no manifest:
  an unmanifested forge-like repo. "Derived" is judged against forge's live
  AGENTS.md headings and gate hook ids, never against a copy in this code.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from forge_dashboard.manifest.models import MANIFEST_FILENAME

CandidateKind = Literal["managed", "unmanifested", "standards-source"]

# Directory names never descended into: dependency trees, build output, caches, OS data.
PRUNED_NAMES = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "target",
        "__pycache__",
        "dist",
        "build",
        "cache",
        "caches",
        "Cache",
        "Caches",
        "AppData",
        "Library",
        "site-packages",
    }
)

_HEADING = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
MIN_SHARED_HEADINGS = 3
MIN_SHARED_HOOKS = 3


def should_descend(name: str) -> bool:
    """Whether a directory with this name may contain projects worth scanning."""
    return not name.startswith(".") and name not in PRUNED_NAMES


@dataclass(frozen=True)
class ForgeSignature:
    """What forge-likeness is measured against, read live from the forge repo."""

    headings: frozenset[str]
    hook_ids: frozenset[str]

    @classmethod
    def read(cls, forge_root: Path) -> ForgeSignature:
        agents = (forge_root / "AGENTS.md").read_text(encoding="utf-8")
        return cls(headings=frozenset(_HEADING.findall(agents)), hook_ids=frozenset(hook_ids(forge_root)))


def hook_ids(repo: Path) -> list[str]:
    """Hook ids in a repo's `.pre-commit-config.yaml`, in file order; empty when absent or unreadable YAML."""
    config_path = repo / ".pre-commit-config.yaml"
    if not config_path.is_file():
        return []
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return []
    if not isinstance(config, dict):
        return []
    return [
        str(hook["id"])
        for repo_entry in config.get("repos") or []
        if isinstance(repo_entry, dict)
        for hook in repo_entry.get("hooks") or []
        if isinstance(hook, dict) and "id" in hook
    ]


def is_forge_repo(directory: Path) -> bool:
    """The standards source's signature: pyproject name `forge` plus `src/forge/gates.py`."""
    pyproject = directory / "pyproject.toml"
    if not pyproject.is_file() or not (directory / "src" / "forge" / "gates.py").is_file():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    project = data.get("project")
    return isinstance(project, dict) and project.get("name") == "forge"


def is_forge_like(directory: Path, signature: ForgeSignature) -> bool:
    """AGENTS.md shares forge's section headings and the gate config shares forge's hook ids."""
    agents = directory / "AGENTS.md"
    if not agents.is_file():
        return False
    try:
        headings = set(_HEADING.findall(agents.read_text(encoding="utf-8")))
    except UnicodeDecodeError:
        return False
    shared_headings = len(headings & signature.headings)
    shared_hooks = len(set(hook_ids(directory)) & signature.hook_ids)
    return shared_headings >= MIN_SHARED_HEADINGS and shared_hooks >= MIN_SHARED_HOOKS


def classify(directory: Path, signature: ForgeSignature | None) -> CandidateKind | None:
    """Decide whether `directory` is a project, and which kind."""
    if is_forge_repo(directory):
        return "standards-source"
    if (directory / MANIFEST_FILENAME).is_file():
        return "managed"
    if signature is not None and is_forge_like(directory, signature):
        return "unmanifested"
    return None


def looks_relevant(names: Iterable[str]) -> bool:
    """Cheap pre-filter on a directory listing before reading any file."""
    present = set(names)
    return MANIFEST_FILENAME in present or "AGENTS.md" in present or "pyproject.toml" in present
