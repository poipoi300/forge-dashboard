"""Compliance against forge's standards, read live from the forge repo on every evaluation.

Nothing of forge's content is copied here: section texts, tool bytes, hook ids
and stages, the ruff selection, and the .gitignore entries are all read from
the forge repo when a check runs. What this module owns is the rule set
(which aspects are compared, and how) and the manifest contract's language
hook-id conventions, which the dashboard itself defines.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from forge_dashboard.core.ui import ui
from forge_dashboard.manifest import load_manifest
from forge_dashboard.manifest.models import MANIFEST_FILENAME

CheckResult = Literal["pass", "fail", "n/a"]
FixKind = Literal["deterministic", "agent"]

# The contract's hook-id conventions per language (docs/project-manifest.md, "Gate hook-id conventions").
LANGUAGE_HOOKS: dict[str, tuple[str, ...]] = {
    "python": ("ruff-format", "ruff-check", "mypy"),
    "rust": ("cargo-fmt", "cargo-clippy"),
    "typescript": ("biome", "tsc"),
}
PUSH_HOOK = "tests-and-diff-coverage"
# Evidence that CI runs a gate: a command fragment per conventional hook id.
CI_EVIDENCE: dict[str, tuple[str, ...]] = {
    "ruff-format": ("ruff format",),
    "ruff-check": ("ruff check",),
    "mypy": ("mypy",),
    "biome": ("biome",),
    "tsc": ("tsc",),
    "cargo-fmt": ("cargo fmt",),
    "cargo-clippy": ("cargo clippy",),
    PUSH_HOOK: ("coverage_gate", "diff-cover"),
}
TOOL_FILES = ("tools/repo_hygiene.py", "tools/coverage_gate.sh")
README_RUN_HEADING = re.compile(
    r"^#{1,3} .*\b(setup|install|installation|run|running|usage|getting started|quick ?start|launch)\b",
    re.IGNORECASE | re.MULTILINE,
)
_SECTION = re.compile(r"^## (.+?)\s*$", re.MULTILINE)


class ComplianceCheck(BaseModel):
    id: str = Field(json_schema_extra=ui(format="code"))
    title: str
    result: CheckResult = Field(
        json_schema_extra=ui(
            format="badge",
            enum_labels={"pass": "pass", "fail": "fail", "n/a": "n/a"},
            tones={"pass": "good", "fail": "bad", "n/a": "neutral"},
        )
    )
    detail: str = ""
    fix: FixKind = Field(
        json_schema_extra=ui(format="badge", tones={"deterministic": "info", "agent": "neutral"}, short_label="fix")
    )
    diff: str | None = Field(None, json_schema_extra=ui(format="code"))


class ComplianceReport(BaseModel):
    checks: list[ComplianceCheck] = Field(json_schema_extra=ui(columns=["result", "title", "fix", "detail"]))
    passing: int
    total: int
    template_revision: str | None = Field(None, json_schema_extra=ui(format="code"))
    forge_head: str | None = Field(None, json_schema_extra=ui(format="code"))

    @property
    def failing(self) -> list[ComplianceCheck]:
        return [check for check in self.checks if check.result == "fail"]


def sections(markdown: str) -> dict[str, str]:
    """`## Heading` -> the section text including its heading, up to the next `## `."""
    found: dict[str, str] = {}
    matches = list(_SECTION.finditer(markdown))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        found[match.group(1)] = markdown[match.start() : end].rstrip() + "\n"
    return found


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def _unified(expected: str, actual: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            actual.splitlines(keepends=True), expected.splitlines(keepends=True), f"project/{name}", f"forge/{name}"
        )
    )


def hook_stages(repo: Path) -> dict[str, tuple[str, ...]]:
    """Hook id -> stages from `.pre-commit-config.yaml`."""
    path = repo / ".pre-commit-config.yaml"
    text = _read(path)
    if text is None:
        return {}
    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(config, dict):
        return {}
    default = tuple(str(stage) for stage in config.get("default_stages") or ("pre-commit",))
    stages: dict[str, tuple[str, ...]] = {}
    for entry in config.get("repos") or []:
        for hook in (entry or {}).get("hooks") or []:
            if isinstance(hook, dict) and "id" in hook:
                stages[str(hook["id"])] = tuple(str(stage) for stage in hook.get("stages") or default)
    return stages


def _ruff_select(pyproject: Path) -> set[str] | None:
    text = _read(pyproject)
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    select = data.get("tool", {}).get("ruff", {}).get("lint", {}).get("select")
    return {str(code) for code in select} if isinstance(select, list) else None


def _mypy_strict(pyproject: Path) -> bool:
    text = _read(pyproject)
    if text is None:
        return False
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return data.get("tool", {}).get("mypy", {}).get("strict") is True


def gitignore_entries(text: str) -> list[str]:
    """Non-empty, non-comment lines of a .gitignore."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def hooks_dir(repo: Path) -> Path:
    """The hooks directory git uses for this repo (`core.hooksPath` aside, `.git/hooks`; worktree-aware)."""
    git = repo / ".git"
    if git.is_file():
        pointer = (_read(git) or "").strip().removeprefix("gitdir:").strip()
        common = Path(pointer)
        common_dir = common / "commondir"
        if common_dir.is_file():
            return (common / (_read(common_dir) or "").strip()).resolve() / "hooks"
        return common / "hooks"
    return git / "hooks"


@dataclass(frozen=True)
class ForgeSource:
    """The live forge repo, as the rules read it."""

    root: Path

    def text(self, relative: str) -> str | None:
        return _read(self.root / relative)

    def base_hooks(self) -> dict[str, tuple[str, ...]]:
        """forge's hooks minus the language-specific ones."""
        language_ids = {hook for hooks in LANGUAGE_HOOKS.values() for hook in hooks}
        return {hook: stages for hook, stages in hook_stages(self.root).items() if hook not in language_ids}


def evaluate(
    project: Path, forge: ForgeSource, *, travelling_sections: Sequence[str], forge_head: str | None
) -> ComplianceReport:
    """Run every compliance check on a project directory (a checkout or a worktree)."""
    load = load_manifest(project)
    manifest = load.manifest
    languages = list(manifest.project.languages) if manifest is not None else []
    checks: list[ComplianceCheck] = []

    def add(
        check_id: str, title: str, result: CheckResult, fix: FixKind, detail: str = "", diff: str | None = None
    ) -> None:
        checks.append(ComplianceCheck(id=check_id, title=title, result=result, detail=detail, fix=fix, diff=diff))

    # 1. manifest
    if not (project / MANIFEST_FILENAME).is_file():
        add("manifest", "forge.project.toml present and valid", "fail", "agent", "no forge.project.toml")
    elif manifest is None:
        first = load.issues[0].render(MANIFEST_FILENAME) if load.issues else "invalid"
        add("manifest", "forge.project.toml present and valid", "fail", "agent", f"{len(load.issues)} errors; {first}")
    else:
        add("manifest", "forge.project.toml present and valid", "pass", "agent")

    # 2-3. AGENTS.md and its travelling sections
    agents = _read(project / "AGENTS.md")
    forge_agents = forge.text("AGENTS.md") or ""
    add(
        "agents-md",
        "AGENTS.md present",
        "pass" if agents is not None else "fail",
        "agent",
        "" if agents is not None else "no AGENTS.md",
    )
    if agents is None:
        add("agents-sections", "AGENTS.md working rules match forge", "fail", "deterministic", "no AGENTS.md")
    else:
        ours, theirs = sections(agents), sections(forge_agents)
        differing = [name for name in travelling_sections if name in theirs and ours.get(name) != theirs[name]]
        diff = "".join(_unified(theirs[name], ours.get(name, ""), f"AGENTS.md#{name}") for name in differing)
        detail = f"differs from forge: {', '.join(differing)}" if differing else ""
        add(
            "agents-sections",
            "AGENTS.md working rules match forge",
            "fail" if differing else "pass",
            "deterministic",
            detail,
            diff or None,
        )

    # 4. CLAUDE.md
    claude = _read(project / "CLAUDE.md")
    imports = claude is not None and any(line.strip() == "@AGENTS.md" for line in claude.splitlines())
    add(
        "claude-md",
        "CLAUDE.md imports AGENTS.md",
        "pass" if imports else "fail",
        "deterministic",
        "" if imports else ("no CLAUDE.md" if claude is None else "CLAUDE.md does not import @AGENTS.md"),
    )

    # 5. tools
    mismatched = [
        name
        for name in TOOL_FILES
        if (project / name).is_file() is False or (project / name).read_bytes() != (forge.root / name).read_bytes()
    ]
    add(
        "tools",
        "tools/ hygiene and coverage scripts identical to forge's",
        "fail" if mismatched else "pass",
        "deterministic",
        f"missing or different: {', '.join(mismatched)}" if mismatched else "",
    )

    # 6. base hooks and stages
    project_hooks = hook_stages(project)
    base = forge.base_hooks()
    missing = sorted(hook for hook in base if hook not in project_hooks)
    wrong_stage = sorted(
        hook for hook, stages in base.items() if hook in project_hooks and project_hooks[hook] != stages
    )
    problems = [f"missing: {', '.join(missing)}"] if missing else []
    problems += [f"stage differs: {', '.join(wrong_stage)}"] if wrong_stage else []
    add(
        "base-hooks",
        "forge's base hooks with matching stages",
        "fail" if problems else "pass",
        "agent",
        "; ".join(problems),
    )

    # 7. language hooks
    if manifest is None:
        add("language-hooks", "conventional gate hooks per language", "n/a", "agent", "needs a valid manifest")
    else:
        wanted = [hook for language in languages for hook in LANGUAGE_HOOKS.get(language, ())]
        lacking = [hook for hook in wanted if hook not in project_hooks]
        add(
            "language-hooks",
            "conventional gate hooks per language",
            "fail" if lacking else "pass",
            "agent",
            f"missing: {', '.join(lacking)}" if lacking else "",
        )

    # 8. installed hooks
    hooks = hooks_dir(project)
    installed = [kind for kind in ("pre-commit", "pre-push") if "pre-commit" in (_read(hooks / kind) or "")]
    absent = [kind for kind in ("pre-commit", "pre-push") if kind not in installed]
    add(
        "hooks-installed",
        "pre-commit and pre-push hooks installed",
        "fail" if absent else "pass",
        "deterministic",
        f"not installed: {', '.join(absent)}" if absent else "",
    )

    # 9. CI
    workflows = sorted((project / ".github" / "workflows").glob("*.y*ml"))
    ci_text = "\n".join(_read(path) or "" for path in workflows)
    enforced = [hook for hook in project_hooks if hook in CI_EVIDENCE]
    unrun = [hook for hook in enforced if not any(fragment in ci_text for fragment in CI_EVIDENCE[hook])]
    if not workflows:
        add("ci", "CI workflow runs the CI-enforced gates", "fail", "agent", "no workflow in .github/workflows")
    else:
        add(
            "ci",
            "CI workflow runs the CI-enforced gates",
            "fail" if unrun else "pass",
            "agent",
            f"not run in CI: {', '.join(unrun)}" if unrun else "",
        )

    # 10. python strictness
    if "python" not in languages:
        add("python-strict", "ruff and mypy at least as strict as forge", "n/a", "agent", "not a Python project")
    else:
        forge_select = _ruff_select(forge.root / "pyproject.toml") or set()
        project_select = _ruff_select(project / "pyproject.toml") or set()
        lacking_rules = sorted(forge_select - project_select)
        strict = _mypy_strict(project / "pyproject.toml")
        issues = [f"ruff missing: {', '.join(lacking_rules)}"] if lacking_rules else []
        issues += [] if strict else ["mypy strict is not enabled"]
        add(
            "python-strict",
            "ruff and mypy at least as strict as forge",
            "fail" if issues else "pass",
            "agent",
            "; ".join(issues),
        )

    # 11. registry mirror test
    test_files = [
        p for folder in ("test", "tests") for p in (project / folder).rglob("*.py") if (project / folder).is_dir()
    ]
    mirrored = any(".pre-commit-config.yaml" in (_read(path) or "") for path in test_files)
    add(
        "registry-test",
        "gate-registry mirror test",
        "pass" if mirrored else "fail",
        "agent",
        "" if mirrored else "no test reads .pre-commit-config.yaml",
    )

    # 12. README
    readme = _read(project / "README.md")
    has_run = readme is not None and README_RUN_HEADING.search(readme) is not None
    add(
        "readme",
        "README with a setup or run section",
        "pass" if has_run else "fail",
        "agent",
        "" if has_run else ("no README.md" if readme is None else "no setup or run section"),
    )

    # 13. .gitignore
    missing_ignores = missing_gitignore_lines(_read(project / ".gitignore") or "", forge.text(".gitignore") or "")
    add(
        "gitignore",
        ".gitignore covers forge's entries",
        "fail" if missing_ignores else "pass",
        "deterministic",
        f"missing: {', '.join(missing_ignores)}" if missing_ignores else "",
    )

    applicable = [check for check in checks if check.result != "n/a"]
    revision = manifest.forge.template_revision if manifest is not None and manifest.forge is not None else None
    return ComplianceReport(
        checks=checks,
        passing=sum(1 for check in applicable if check.result == "pass"),
        total=len(applicable),
        template_revision=revision,
        forge_head=forge_head,
    )


def apply_section_sync(agents_text: str, forge_agents: str, travelling: Iterable[str]) -> str:
    """Replace (or append) each travelling section with forge's text, leaving project sections alone."""
    ours, theirs = sections(agents_text), sections(forge_agents)
    result = agents_text
    for name in travelling:
        if name not in theirs:
            continue
        if name in ours:
            result = result.replace(ours[name], theirs[name])
        else:
            result = result.rstrip("\n") + "\n\n" + theirs[name]
    return result if result.endswith("\n") else result + "\n"


def missing_gitignore_lines(project_ignore: str, forge_ignore: str) -> list[str]:
    """forge's .gitignore entries the project's .gitignore lacks, in forge's order."""
    present = set(gitignore_entries(project_ignore))
    return [entry for entry in gitignore_entries(forge_ignore) if entry not in present]
