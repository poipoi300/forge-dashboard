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
from typing import Literal, cast

import yaml
from markdown_it import MarkdownIt
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
_REUSABLE = re.compile(r"([^/@]+/[^/@]+)/(\.github/workflows/[^@]+)@(.+)")
_GENERATED_LINK = re.compile(r"- \[[^\]]+\]\(https://github\.com/[^/\s]+/[^/\s]+/blob/[0-9a-f]{40}/AGENTS\.md#[\w-]+\)")
_GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?"
)
_MARKDOWN = MarkdownIt("commonmark")
_LINE_END = re.compile(r"\r\n|\r|\n")
_LEGACY_REVISION = re.compile(r"[0-9a-f]{7,40}")
# pre-commit still accepts the old stage names and normalizes them.
_LEGACY_STAGES = {"commit": "pre-commit", "push": "pre-push", "merge-commit": "pre-merge-commit"}
RULES_SECTION = "Working rules from forge"
# pre-commit runs a hook that ends up with no stages at every stage.
EVERY_STAGE = ("every stage",)


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


def _section_spans(markdown: str) -> list[tuple[str | None, int, int]]:
    """`(heading, start, end)` of each level-2 section as CommonMark parses it; other spans have heading None.

    A section runs from its top-level `##` (or setext) heading to the next
    top-level level-1 or level-2 heading; headings inside code blocks, block
    quotes or lists do not split the document.
    """
    # CommonMark's lines end at CR, LF or CRLF only (not at U+2028 and the like).
    starts = [0, *(match.end() for match in _LINE_END.finditer(markdown))]
    tokens = _MARKDOWN.parse(markdown)
    boundaries: list[tuple[str | None, int]] = [(None, 0)]
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0 and token.tag in {"h1", "h2"} and token.map is not None:
            name = tokens[index + 1].content.strip() if token.tag == "h2" else None
            boundaries.append((name, starts[token.map[0]]))
    ends = [start for _, start in boundaries[1:]] + [len(markdown)]
    return [(name, start, end) for (name, start), end in zip(boundaries, ends, strict=True) if end > start]


def sections(markdown: str) -> dict[str, str]:
    """`## Heading` -> the section text including its heading, up to the next `## ` outside fenced code."""
    return {
        name: markdown[start:end].rstrip() + "\n" for name, start, end in _section_spans(markdown) if name is not None
    }


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
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
    """Lower-cased `owner/repo` of a GitHub remote URL (https or ssh), or None for anything else."""
    match = _GITHUB_REMOTE.fullmatch(url.strip())
    return match.group(1).lower() if match else None


def section_anchor(heading: str) -> str:
    """The fragment GitHub gives a plain-text Markdown heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _strings(value: object) -> tuple[str, ...] | None:
    """A YAML list of strings as a tuple, or None for any other shape."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


@dataclass(frozen=True)
class PrecommitConfig:
    """The parts of `.pre-commit-config.yaml` the rules read, in pre-commit's own shapes."""

    repos: list[dict[str, object]]
    default_stages: tuple[str, ...] | None

    def hooks(self, entry: dict[str, object]) -> list[dict[str, object]]:
        return cast("list[dict[str, object]]", entry.get("hooks", []))

    def hook_ids(self) -> set[str]:
        return {str(hook["id"]) for entry in self.repos for hook in self.hooks(entry)}

    def entries_for(self, slug: str) -> list[dict[str, object]]:
        return [
            entry
            for entry in self.repos
            if isinstance(entry.get("repo"), str) and github_slug(str(entry["repo"])) == slug
        ]


def _valid_hook(hook: object) -> bool:
    return (
        isinstance(hook, dict)
        and isinstance(hook.get("id"), str)
        and ("stages" not in hook or _strings(hook["stages"]) is not None)
    )


def precommit_config(repo: Path) -> PrecommitConfig | None:
    """`.pre-commit-config.yaml`, or None when it is missing, unparsable or not shaped as pre-commit requires."""
    config = _yaml(repo / ".pre-commit-config.yaml")
    if not isinstance(config, dict) or not isinstance(repos := config.get("repos"), list):
        return None
    default_stages = _strings(config["default_stages"]) if "default_stages" in config else None
    if "default_stages" in config and default_stages is None:
        return None
    for entry in repos:
        if not isinstance(entry, dict) or not isinstance(entry.get("repo"), str):
            return None
        hooks = entry.get("hooks")
        if not isinstance(hooks, list) or not all(_valid_hook(hook) for hook in hooks):
            return None
    return PrecommitConfig(repos=repos, default_stages=default_stages)


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

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=False)

    def slug(self) -> str | None:
        """forge's lower-cased `owner/repo` on GitHub, from its `origin` remote."""
        result = self._git("config", "--get", "remote.origin.url")
        return github_slug(result.stdout) if result.returncode == 0 else None

    def object_type(self, spec: str) -> str | None:
        """The git type of an object (`commit`, `tag`, `tree`, `blob`), or None when it is missing."""
        result = self._git("cat-file", "-t", spec)
        return result.stdout.strip() if result.returncode == 0 else None

    def is_commit(self, revision: str) -> bool:
        # The pinned object itself: an annotated tag's SHA names a tag, not a commit.
        return self.object_type(revision) == "commit"

    def resolve(self, ref: str) -> str | None:
        result = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return result.stdout.strip() if result.returncode == 0 else None

    def reusable_workflow_at(self, revision: str) -> bool:
        """Whether the revision has forge's gates workflow as a file that declares `workflow_call`."""
        text = self.text_at(revision, FORGE_WORKFLOW)
        try:
            # BaseLoader keeps keys literal; YAML 1.1 would read `on` (and `yes`) as True.
            workflow = yaml.load(text, Loader=yaml.BaseLoader) if text is not None else None
        except yaml.YAMLError:
            return False
        triggers = workflow.get("on") if isinstance(workflow, dict) else None
        if isinstance(triggers, str):
            return triggers == "workflow_call"
        return isinstance(triggers, (list, dict)) and "workflow_call" in triggers

    def rule_sections_at(self, revision: str) -> dict[str, str] | None:
        """forge's AGENTS.md sections at a commit, or None when it is no forge commit with an AGENTS.md file."""
        if not self.is_commit(revision):
            return None
        text = self.text_at(revision, "AGENTS.md")
        return sections(text) if text is not None else None

    def text_at(self, revision: str, relative: str) -> str | None:
        """A UTF-8 file's text at a forge revision, or None when that revision has no such file."""
        if self.object_type(f"{revision}:{relative}") != "blob":
            return None
        result = subprocess.run(
            ["git", "-C", str(self.root), "show", f"{revision}:{relative}"], capture_output=True, check=False
        )
        try:
            return result.stdout.decode("utf-8") if result.returncode == 0 else None
        except UnicodeDecodeError:
            return None

    def published_hooks(self, revision: str) -> dict[str, tuple[str, ...]] | None:
        """Hook id -> declared stages in `.pre-commit-hooks.yaml` at a revision, or None when it has none."""
        text = self.text_at(revision, ".pre-commit-hooks.yaml")
        try:
            manifest = yaml.safe_load(text) if text is not None else None
        except yaml.YAMLError:
            return None
        if not isinstance(manifest, list) or not all(_valid_hook(hook) for hook in manifest):
            return None
        return {str(hook["id"]): _strings(hook.get("stages", [])) or () for hook in manifest}


def _stage_set(stages: Iterable[str]) -> frozenset[str]:
    return frozenset(_LEGACY_STAGES.get(stage, stage) for stage in stages)


def _expected_stages(declared: tuple[str, ...]) -> frozenset[str]:
    # forge's hooks without declared stages are commit-stage gates.
    return _stage_set(declared or ("pre-commit",))


def _effective_stages(
    hook: dict[str, object], declared: tuple[str, ...], default: tuple[str, ...] | None
) -> frozenset[str]:
    # pre-commit's resolution: the config's `stages` replaces the manifest's, even when empty;
    # an empty result takes `default_stages`, and without those the hook runs at every stage.
    stages = _strings(hook["stages"]) or () if "stages" in hook else declared
    return _stage_set(stages or default or EVERY_STAGE)


def _workflow_pins(project: Path, slug: str) -> list[str]:
    folder = project / ".github" / "workflows"
    paths = (
        sorted(path for path in folder.iterdir() if path.suffix in {".yml", ".yaml"} and path.is_file())
        if folder.is_dir()
        else []
    )
    pins: list[str] = []
    for path in paths:
        workflow = _yaml(path)
        jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
        for job in jobs.values() if isinstance(jobs, dict) else []:
            uses = job.get("uses") if isinstance(job, dict) else None
            call = _REUSABLE.fullmatch(uses) if isinstance(uses, str) else None
            if call and call.group(1).lower() == slug and call.group(2) == FORGE_WORKFLOW:
                pins.append(call.group(3))
    return pins


def is_revision(value: str) -> bool:
    """Whether a recorded template revision is a commit ID git can look up (abbreviated ones included)."""
    return _LEGACY_REVISION.fullmatch(value) is not None


def rule_links(slug: str, revision: str, travelling: Iterable[str]) -> str:
    """The AGENTS.md section linking forge's working-rule sections at a revision."""
    base = f"https://github.com/{slug}/blob/{revision}/AGENTS.md"
    lines = "".join(f"- [{name}]({base}#{section_anchor(name)})\n" for name in travelling)
    return f"## {RULES_SECTION}\n\n{lines}"


def _link_targets(markdown: str) -> list[str]:
    """Destinations of the links the Markdown renders (inline, autolinks and used references; not images or code)."""
    return [
        str(child.attrGet("href"))
        for token in _MARKDOWN.parse(markdown)
        for child in token.children or []
        if child.type == "link_open"
    ]


def _linked_revisions(agents: str, slug: str, name: str) -> list[str]:
    """The forge SHAs at which AGENTS.md links section `name` as a rendered link."""
    # CommonMark percent-encodes link destinations, so compare in that form.
    fragment = _MARKDOWN.normalizeLink(f"#{section_anchor(name)}")
    pattern = re.compile(
        rf"https://github\.com/(?i:{re.escape(slug)})/blob/([0-9a-f]{{40}})/AGENTS\.md{re.escape(fragment)}"
    )
    return [match.group(1) for target in _link_targets(agents) if (match := pattern.fullmatch(target))]


def _missing_rule_links(agents: str, slug: str, travelling: Iterable[str], forge: ForgeSource) -> list[str]:
    """Working-rule sections not linked at a forge commit whose AGENTS.md has them."""
    known: dict[str, dict[str, str] | None] = {}
    missing = []
    for name in travelling:
        revisions = _linked_revisions(agents, slug, name)
        for revision in revisions:
            if revision not in known:
                known[revision] = forge.rule_sections_at(revision)
        if not any(name in (known[revision] or {}) for revision in revisions):
            missing.append(name)
    return missing


def _same_text(left: str, right: str) -> bool:
    return " ".join(left.split()) == " ".join(right.split())


def _generated_links(section: str) -> bool:
    body = section.splitlines()[1:]
    return all(not line.strip() or _GENERATED_LINK.fullmatch(line.strip()) for line in body)


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
    forge_agents = forge.text("AGENTS.md")
    theirs = sections(forge_agents or "")
    rule_names = [name for name in travelling_sections if name in theirs]
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
    elif forge_agents is None:
        add("agents-sections", links_title, "fail", "deterministic", "forge's AGENTS.md cannot be read")
    else:
        ours = sections(agents)
        copied = [name for name in rule_names if name in ours and _same_text(ours[name], theirs[name])]
        own = [name for name in rule_names if name in ours and name not in copied]
        unlinked = _missing_rule_links(agents, slug, rule_names, forge)
        problems = [f"copies forge's sections: {', '.join(copied)}"] if copied else []
        problems += [f"has its own sections named like forge's: {', '.join(own)}"] if own else []
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

    # 5. hooks reference forge at one pinned commit that publishes them
    config = precommit_config(project)
    entries = config.entries_for(slug) if config is not None and slug is not None else []
    pin: str | None = None
    published: dict[str, tuple[str, ...]] | None = None
    revs = [entry.get("rev") for entry in entries]
    floating = [str(rev) for rev in revs if not (isinstance(rev, str) and _COMMIT_SHA.fullmatch(rev))]
    distinct = sorted({rev for rev in revs if isinstance(rev, str)})
    if slug is None:
        pins_detail = "forge's checkout has no GitHub origin remote"
    elif config is None:
        pins_detail = "no valid .pre-commit-config.yaml"
    elif not entries:
        pins_detail = f"no https://github.com/{slug} repo in .pre-commit-config.yaml"
    elif floating:
        pins_detail = f"rev {', '.join(floating)} is not a commit SHA"
    elif len(distinct) > 1:
        pins_detail = f"forge is pinned at several revs: {', '.join(distinct)}"
    elif not forge.is_commit(distinct[0]):
        pins_detail = f"rev {distinct[0]} is not a commit in forge's checkout (fetch forge)"
    elif (published := forge.published_hooks(distinct[0])) is None:
        pins_detail = f"forge commit {distinct[0]} publishes no .pre-commit-hooks.yaml"
    else:
        pins_detail, pin = "", distinct[0]
    add(
        "forge-hooks",
        "hooks reference forge at a pinned commit",
        "fail" if pins_detail else "pass",
        "agent",
        pins_detail,
    )

    # 6. every hook forge publishes at the pin, referenced from forge, at forge's stages
    if config is None or pin is None or published is None:
        problems = ["needs one valid forge pin (see forge-hooks)"]
    else:
        referenced = [hook for entry in entries for hook in config.hooks(entry)]
        ids = {str(hook["id"]) for hook in referenced}
        missing = sorted(set(published) - ids)
        unknown = sorted(ids - set(published))
        wrong_stage = sorted(
            {
                str(hook["id"])
                for hook in referenced
                if str(hook["id"]) in published
                and _effective_stages(hook, published[str(hook["id"])], config.default_stages)
                != _expected_stages(published[str(hook["id"])])
            }
        )
        problems = [f"not referenced from forge: {', '.join(missing)}"] if missing else []
        problems += [f"not published by forge at {pin}: {', '.join(unknown)}"] if unknown else []
        problems += [f"stage differs: {', '.join(wrong_stage)}"] if wrong_stage else []
    add(
        "base-hooks",
        "forge's published hooks with matching stages",
        "fail" if problems else "pass",
        "agent",
        "; ".join(problems),
    )
    project_hooks = config.hook_ids() if config is not None else set()

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
    ci_pins = _workflow_pins(project, slug) if slug is not None else []
    ci_floating = sorted({rev for rev in ci_pins if not _COMMIT_SHA.fullmatch(rev)})
    if slug is None:
        ci_detail = "forge's checkout has no GitHub origin remote"
    elif not ci_pins:
        ci_detail = f"no job uses {slug}/{FORGE_WORKFLOW}"
    elif ci_floating:
        ci_detail = f"gates.yml@{', '.join(ci_floating)} is not a commit SHA"
    elif pin is None:
        ci_detail = "no single valid forge hook pin to compare with (see forge-hooks)"
    elif disagreeing := sorted(set(ci_pins) - {pin}):
        ci_detail = f"pins differ: hooks {pin}, CI {', '.join(disagreeing)}"
    elif not forge.reusable_workflow_at(pin):
        ci_detail = f"forge commit {pin} has no reusable {FORGE_WORKFLOW}"
    else:
        ci_detail = ""
    add(
        "ci",
        "CI calls forge's gates workflow at the pinned commit",
        "fail" if ci_detail else "pass",
        "agent",
        ci_detail,
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


def apply_rule_links(
    agents_text: str,
    slug: str,
    revision: str,
    travelling: Iterable[str],
    target: dict[str, str],
    recorded: dict[str, str] | None = None,
) -> str:
    """Drop copies of forge's working-rule sections and link them at `revision` instead.

    Only sections forge has at `revision` (`target`) are linked or removed. A
    section is a copy when its text matches forge's at the target or at the
    revision the project last aligned to (`recorded`), ignoring whitespace; a
    project's own section under one of those headings stays for a person to
    resolve. The links live in one generated section, recognised by its heading
    and its link-only body and replaced on each alignment. Everything else
    is left byte for byte.
    """
    names = [name for name in travelling if name in target]
    versions = [target, *([recorded] if recorded else [])]

    def dropped(name: str | None, text: str) -> bool:
        if name in names:
            return any(name in version and _same_text(text, version[name]) for version in versions)
        return name == RULES_SECTION and _generated_links(text)

    kept = "".join(
        agents_text[start:end]
        for name, start, end in _section_spans(agents_text)
        if not dropped(name, agents_text[start:end])
    ).rstrip("\n")
    return (kept + "\n\n" if kept else "") + rule_links(slug, revision, names)


def missing_gitignore_lines(project_ignore: str, forge_ignore: str) -> list[str]:
    """forge's .gitignore entries the project's .gitignore lacks, in forge's order."""
    present = set(gitignore_entries(project_ignore))
    return [entry for entry in gitignore_entries(forge_ignore) if entry not in present]
