"""Compliance against forge's standards, read live from the forge repo on every evaluation.

Nothing of forge's content is copied here: section names, published hook ids
and stages, the ruff selection, and the .gitignore entries are all read from
the forge repo when a check runs. What this module owns is the rule set
(which aspects are compared, and how) and the manifest contract's language
hook-id conventions, which the dashboard itself defines.

Projects consume forge by reference, never by copy: their hooks name forge's
repository at a pinned commit, their CI calls forge's reusable workflow at the
same commit, and their AGENTS.md links forge's working-rule sections.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from forge_dashboard.core.ui import ui
from forge_dashboard.domain.launch import readme_run_section
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
FORGE_WORKFLOW = ".github/workflows/gates.yml"
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?"
)
_SECTION = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
RULES_SECTION = "Working rules"


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


def _yaml(path: Path) -> object:
    text = _read(path)
    if text is None:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def github_slug(url: str) -> str | None:
    """`owner/repo` of a GitHub remote URL (https or ssh), or None for anything else."""
    match = _GITHUB_REMOTE.fullmatch(url.strip())
    return match.group(1) if match else None


def section_anchor(heading: str) -> str:
    """The fragment GitHub gives a Markdown heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def precommit_repos(repo: Path) -> list[dict[str, object]]:
    """The `repos` entries of `.pre-commit-config.yaml`, or none when it is missing or malformed."""
    config = _yaml(repo / ".pre-commit-config.yaml")
    entries = config.get("repos") if isinstance(config, dict) else None
    return [entry for entry in entries or [] if isinstance(entry, dict)]


def hook_stages(
    repo: Path, *, remote: str | None = None, published: dict[str, tuple[str, ...]] | None = None
) -> dict[str, tuple[str, ...]]:
    """Hook id -> stages from `.pre-commit-config.yaml`.

    A hook without its own `stages` takes them from the hook repository's
    manifest (`published`, for hooks under the `remote` repo slug), then from
    `default_stages`.
    """
    config = _yaml(repo / ".pre-commit-config.yaml")
    if not isinstance(config, dict):
        return {}
    default = tuple(str(stage) for stage in config.get("default_stages") or ("pre-commit",))
    stages: dict[str, tuple[str, ...]] = {}
    for entry in precommit_repos(repo):
        manifest = published if remote is not None and github_slug(str(entry.get("repo", ""))) == remote else None
        hooks = entry.get("hooks")
        for hook in hooks if isinstance(hooks, list) else []:
            if isinstance(hook, dict) and "id" in hook:
                hook_id = str(hook["id"])
                fallback = (manifest or {}).get(hook_id, default)
                stages[hook_id] = tuple(str(stage) for stage in hook.get("stages") or fallback)
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

    def slug(self) -> str | None:
        """forge's `owner/repo` on GitHub, from its `origin` remote."""
        result = subprocess.run(
            ["git", "-C", str(self.root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
        return github_slug(result.stdout) if result.returncode == 0 else None

    def published_hooks(self) -> dict[str, tuple[str, ...]]:
        """Hook id -> stages of the hooks forge publishes in `.pre-commit-hooks.yaml`."""
        manifest = _yaml(self.root / ".pre-commit-hooks.yaml")
        hooks = (
            [hook for hook in manifest if isinstance(hook, dict) and "id" in hook] if isinstance(manifest, list) else []
        )
        return {str(hook["id"]): tuple(str(stage) for stage in hook.get("stages") or ("pre-commit",)) for hook in hooks}


def _forge_pins(project: Path, slug: str) -> list[str]:
    return [
        str(entry.get("rev", ""))
        for entry in precommit_repos(project)
        if github_slug(str(entry.get("repo", ""))) == slug
    ]


def _forge_hook_ids(project: Path, slug: str) -> set[str]:
    ids: set[str] = set()
    for entry in precommit_repos(project):
        hooks = entry.get("hooks")
        if github_slug(str(entry.get("repo", ""))) == slug and isinstance(hooks, list):
            ids |= {str(hook["id"]) for hook in hooks if isinstance(hook, dict) and "id" in hook}
    return ids


def _workflow_pins(project: Path, slug: str) -> list[str]:
    prefix = f"{slug}/{FORGE_WORKFLOW}@"
    pins: list[str] = []
    for path in sorted((project / ".github" / "workflows").glob("*.y*ml")):
        workflow = _yaml(path)
        jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
        for job in jobs.values() if isinstance(jobs, dict) else []:
            uses = job.get("uses") if isinstance(job, dict) else None
            if isinstance(uses, str) and uses.startswith(prefix):
                pins.append(uses.removeprefix(prefix))
    return pins


def rule_links(slug: str, revision: str, travelling: Iterable[str]) -> str:
    """The AGENTS.md section linking forge's working-rule sections at a revision."""
    base = f"https://github.com/{slug}/blob/{revision}/AGENTS.md"
    lines = "".join(f"- [{name}]({base}#{section_anchor(name)})\n" for name in travelling)
    return f"## {RULES_SECTION}\n\n{lines}"


def _missing_rule_links(agents: str, slug: str, travelling: Iterable[str]) -> list[str]:
    base = re.escape(f"https://github.com/{slug}/blob/")
    return [
        name
        for name in travelling
        if not re.search(rf"{base}[0-9a-f]{{40}}/AGENTS\.md#{re.escape(section_anchor(name))}\)", agents)
    ]


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

    # 2-3. AGENTS.md, linking forge's working-rule sections
    agents = _read(project / "AGENTS.md")
    slug = forge.slug()
    rule_names = [name for name in travelling_sections if name in sections(forge.text("AGENTS.md") or "")]
    add(
        "agents-md",
        "AGENTS.md present",
        "pass" if agents is not None else "fail",
        "agent",
        "" if agents is not None else "no AGENTS.md",
    )
    links_title = "AGENTS.md links forge's working rules"
    if agents is None:
        add("agents-sections", links_title, "fail", "deterministic", "no AGENTS.md")
    elif slug is None:
        add("agents-sections", links_title, "fail", "deterministic", "forge's checkout has no GitHub origin remote")
    else:
        copied = [name for name in rule_names if name in sections(agents)]
        unlinked = _missing_rule_links(agents, slug, rule_names)
        problems = [f"copies forge's sections: {', '.join(copied)}"] if copied else []
        problems += [f"no pinned link to: {', '.join(unlinked)}"] if unlinked else []
        add("agents-sections", links_title, "fail" if problems else "pass", "deterministic", "; ".join(problems))

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

    # 5. hooks reference forge at a pinned commit
    hook_pins = _forge_pins(project, slug) if slug is not None else []
    pins_title = "hooks reference forge at a pinned commit"
    if slug is None:
        add("forge-hooks", pins_title, "fail", "agent", "forge's checkout has no GitHub origin remote")
    elif not hook_pins:
        add("forge-hooks", pins_title, "fail", "agent", f"no https://github.com/{slug} repo in .pre-commit-config.yaml")
    else:
        floating = [rev for rev in hook_pins if not _COMMIT_SHA.fullmatch(rev)]
        detail = f"rev {', '.join(floating)} is not a commit SHA" if floating else ""
        add("forge-hooks", pins_title, "fail" if floating else "pass", "agent", detail)

    # 6. forge's published hooks, taken from forge, with matching stages
    published = forge.published_hooks()
    project_hooks = hook_stages(project, remote=slug, published=published)
    forge_ids = _forge_hook_ids(project, slug) if slug is not None else set()
    missing = sorted(hook for hook in published if hook not in forge_ids)
    wrong_stage = sorted(
        hook for hook, stages in published.items() if hook in forge_ids and project_hooks.get(hook) != stages
    )
    problems = [f"not referenced from forge: {', '.join(missing)}"] if missing else []
    problems += [f"stage differs: {', '.join(wrong_stage)}"] if wrong_stage else []
    add(
        "base-hooks",
        "forge's published hooks with matching stages",
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

    # 9. CI calls forge's reusable workflow at the hooks' commit
    ci_title = "CI calls forge's gates workflow at the pinned commit"
    ci_pins = _workflow_pins(project, slug) if slug is not None else []
    if slug is None:
        add("ci", ci_title, "fail", "agent", "forge's checkout has no GitHub origin remote")
    elif not ci_pins:
        add("ci", ci_title, "fail", "agent", f"no job uses {slug}/{FORGE_WORKFLOW}")
    else:
        floating = [rev for rev in ci_pins if not _COMMIT_SHA.fullmatch(rev)]
        disagreeing = sorted(set(ci_pins) - set(hook_pins)) if hook_pins else []
        if floating:
            detail = f"gates.yml@{', '.join(floating)} is not a commit SHA"
        elif disagreeing:
            detail = f"pins differ: hooks {', '.join(sorted(set(hook_pins)))}, CI {', '.join(disagreeing)}"
        else:
            detail = ""
        add("ci", ci_title, "fail" if detail else "pass", "agent", detail)

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
    has_run = readme is not None and readme_run_section(readme) is not None
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


def apply_rule_links(agents_text: str, slug: str, revision: str, travelling: Iterable[str]) -> str:
    """Drop copies of forge's working-rule sections and link them at `revision` instead.

    The links live in one generated `## Working rules` section, replaced
    wholesale on each alignment; every other section is left alone.
    """
    names = list(travelling)
    result = agents_text
    for name, text in sections(agents_text).items():
        if name in names or name == RULES_SECTION:
            result = result.replace(text, "")
    return result.rstrip("\n") + "\n\n" + rule_links(slug, revision, names)


def missing_gitignore_lines(project_ignore: str, forge_ignore: str) -> list[str]:
    """forge's .gitignore entries the project's .gitignore lacks, in forge's order."""
    present = set(gitignore_entries(project_ignore))
    return [entry for entry in gitignore_entries(forge_ignore) if entry not in present]
