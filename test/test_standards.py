"""Standards: compliance against a live (fixture) forge, bring-up, adopt, and the template revision."""

import os
import shutil
import textwrap
import tomllib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FORGE_PYPROJECT, FORGE_URL, MANIFEST, Git, write_files, write_hook
from testing_support import fake_harness_config, wait_for

from forge_dashboard.branchwork import BranchRunState
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, ProjectRef
from forge_dashboard.domain import standards as rules
from forge_dashboard.domain.harness import HarnessConfig
from forge_dashboard.domain.standards import ForgeSource
from forge_dashboard.services.agents import AgentsConfig, AgentsService, BranchRunInput, BranchRunsInput
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService
from forge_dashboard.services.standards import StandardsService, set_template_revision, standards_brief

TRAVELLING = ["A note from the maintainer", "Verifying", "Commits", "Taste"]


@pytest.fixture
def forge_repo(forge_repo: Path, git: Git) -> Path:
    """The fixture forge, published on GitHub so projects can reference it."""
    git(forge_repo, "remote", "add", "origin", f"{FORGE_URL}.git")
    return forge_repo


def _results(report: rules.ComplianceReport) -> dict[str, str]:
    return {check.id: check.result for check in report.checks}


def test_a_bare_repo_fails_almost_everything(forge_repo: Path, tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    report = rules.evaluate(bare, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    results = _results(report)
    assert results["manifest"] == "fail" and results["language-hooks"] == "n/a" and results["python-strict"] == "n/a"
    assert report.passing == 0 and report.total == 11
    assert {check.fix for check in report.checks if check.id in ("agents-sections", "claude-md", "gitignore")} == {
        "deterministic"
    }


def _links(revision: str) -> str:
    base = f"{FORGE_URL}/blob/{revision}/AGENTS.md"
    return (
        "## Working rules from forge\n\n"
        f"- [A note from the maintainer]({base}#a-note-from-the-maintainer)\n"
        f"- [Verifying]({base}#verifying)\n- [Commits]({base}#commits)\n- [Taste]({base}#taste)\n"
    )


def _referencing_project(
    tmp_path: Path, git: Git, *, hooks_rev: str, ci_rev: str, links_rev: str | None = None
) -> Path:
    project = tmp_path / "referencing"
    write_files(
        project,
        {
            "forge.project.toml": MANIFEST.replace("{name}", "Referencing").replace("{project_id}", "referencing"),
            "AGENTS.md": f"# Referencing\n\nIntro.\n\n{_links(links_rev or hooks_rev)}",
            "CLAUDE.md": "@AGENTS.md\n",
            "README.md": "# Referencing\n\n## Setup\n\nRun it.\n",
            ".gitignore": "__pycache__/\n.venv/\n.coverage\n",
            "pyproject.toml": FORGE_PYPROJECT.replace('name = "forge"', 'name = "referencing"'),
            ".pre-commit-config.yaml": f"""
                default_stages: [pre-commit]
                repos:
                  - repo: {FORGE_URL}
                    rev: {hooks_rev}
                    hooks:
                      - id: trailing-whitespace
                      - id: end-of-file-fixer
                      - id: check-merge-conflict
                      - id: check-toml
                      - id: tests-and-diff-coverage
                        args: [--python]
                  - repo: local
                    hooks:
                      - id: ruff-format
                        name: ruff format
                        entry: uv run ruff format
                        language: system
                      - id: ruff-check
                        name: ruff check
                        entry: uv run ruff check
                        language: system
                      - id: mypy
                        name: mypy
                        entry: uv run mypy .
                        language: system
                """,
            ".github/workflows/ci.yml": f"""
                on: [push, pull_request]
                jobs:
                  gates:
                    uses: owner/forge/.github/workflows/gates.yml@{ci_rev}
                    with:
                      os: '["ubuntu-24.04"]'
                """,
            "test/test_gates.py": "# reads .pre-commit-config.yaml\n",
        },
    )
    git.init(project)
    git.commit_all(project, "Init")
    for kind in ("pre-commit", "pre-push"):
        write_hook(project / ".git" / "hooks" / kind, "#!/bin/sh\n# File generated by pre-commit\n")
    return project


def test_a_project_referencing_forge_at_one_pinned_revision_passes(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    revision = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=revision, ci_rev=revision)
    report = rules.evaluate(project, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    assert {k: v for k, v in _results(report).items() if v != "pass"} == {}
    assert report.passing == report.total == 13


def test_copies_of_forge_fail_every_reference_check(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    project = tmp_path / "copied"
    shutil.copytree(forge_repo, project, ignore=shutil.ignore_patterns(".git"))
    write_files(project, {"forge.project.toml": MANIFEST.replace("{name}", "Copied").replace("{project_id}", "copied")})
    git.init(project)
    git.commit_all(project, "Init")
    report = rules.evaluate(project, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    details = {check.id: check.detail for check in report.checks if check.result == "fail"}
    assert {"agents-sections", "forge-hooks", "base-hooks", "ci"} <= set(details)
    assert (
        "copies forge's sections: A note from the maintainer, Verifying, Commits, Taste" in details["agents-sections"]
    )
    assert details["forge-hooks"] == f"no {FORGE_URL} repo in .pre-commit-config.yaml"
    assert details["ci"] == "no job uses owner/forge/.github/workflows/gates.yml"


@pytest.mark.parametrize(
    ("hooks_rev", "ci_rev", "failing"),
    [
        (
            "main",
            "HEAD",
            {
                "forge-hooks": "rev main is not a commit SHA",
                "base-hooks": "needs one valid forge pin (see forge-hooks)",
                "ci": "no single valid forge hook pin to compare with (see forge-hooks)",
            },
        ),
        ("HEAD", "v1", {"ci": "gates.yml@v1 is not a commit SHA"}),
        ("HEAD", "0" * 40, {"ci": "pins differ: hooks HEAD, CI 0000000000000000000000000000000000000000"}),
    ],
)
def test_floating_or_disagreeing_pins_fail(
    forge_repo: Path, tmp_path: Path, git: Git, hooks_rev: str, ci_rev: str, failing: dict[str, str]
) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(
        tmp_path, git, hooks_rev=hooks_rev.replace("HEAD", head), ci_rev=ci_rev.replace("HEAD", head), links_rev=head
    )
    report = rules.evaluate(project, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    found = {check.id: check.detail for check in report.checks if check.result == "fail"}
    assert found == {key: value.replace("HEAD", head) for key, value in failing.items()}


def _evaluate(project: Path, forge_repo: Path) -> dict[str, str]:
    report = rules.evaluate(project, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    return {check.id: check.detail for check in report.checks if check.result == "fail"}


def _set_precommit(project: Path, text: str) -> None:
    (project / ".pre-commit-config.yaml").write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


FORGE_HOOK_IDS = ["trailing-whitespace", "end-of-file-fixer", "check-merge-conflict", "check-toml"]


def _forge_entry(revision: str, *, repo: str = FORGE_URL, push_stages: str = "") -> str:
    hooks = "".join(f"\n      - id: {hook}" for hook in FORGE_HOOK_IDS)
    return f"""
  - repo: {repo}
    rev: {revision}
    hooks:{hooks}
      - id: tests-and-diff-coverage{push_stages}"""


@pytest.mark.parametrize(
    ("default_stages", "push_stages", "differs"),
    [
        ("[pre-commit, pre-push]", "", "check-merge-conflict, check-toml, end-of-file-fixer, trailing-whitespace"),
        (None, "", "check-merge-conflict, check-toml, end-of-file-fixer, trailing-whitespace"),
        ("[pre-commit]", "\n        stages: []", "tests-and-diff-coverage"),
        ("[pre-commit]", "\n        stages: [pre-push]", None),
    ],
)
def test_hook_stages_resolve_as_pre_commit_resolves_them(
    forge_repo: Path, tmp_path: Path, git: Git, default_stages: str | None, push_stages: str, differs: str | None
) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    defaults = f"default_stages: {default_stages}\n" if default_stages else ""
    _set_precommit(project, defaults + "repos:" + _forge_entry(head, push_stages=push_stages) + "\n")
    failing = _evaluate(project, forge_repo)
    assert failing.get("base-hooks") == (f"stage differs: {differs}" if differs else None)


def test_a_local_hook_cannot_mask_a_forge_hook_at_the_wrong_stage(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    forge_entry = _forge_entry(head, push_stages="\n        stages: [pre-commit]")
    local = """
  - repo: local
    hooks:
      - id: tests-and-diff-coverage
        name: tests
        entry: run-tests
        language: system
        stages: [pre-push]"""
    for order in (forge_entry + local, local + forge_entry):
        _set_precommit(project, "default_stages: [pre-commit]\nrepos:" + order + "\n")
        assert _evaluate(project, forge_repo)["base-hooks"] == "stage differs: tests-and-diff-coverage"


def test_several_forge_pins_leave_nothing_to_compare_ci_with(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    other = "ab" * 20
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    _set_precommit(project, "default_stages: [pre-commit]\nrepos:" + _forge_entry(head) + _forge_entry(other) + "\n")
    failing = _evaluate(project, forge_repo)
    assert failing["forge-hooks"] == f"forge is pinned at several revs: {', '.join(sorted([head, other]))}"
    assert failing["ci"] == "no single valid forge hook pin to compare with (see forge-hooks)"


def test_a_pin_must_be_a_forge_commit_that_publishes_the_hooks_and_workflow(
    forge_repo: Path, tmp_path: Path, git: Git
) -> None:
    unknown = "cd" * 20
    project = _referencing_project(tmp_path, git, hooks_rev=unknown, ci_rev=unknown)
    assert (
        _evaluate(project, forge_repo)["forge-hooks"]
        == f"rev {unknown} is not a commit in forge's checkout (fetch forge)"
    )
    git(forge_repo, "rm", "-q", ".pre-commit-hooks.yaml", ".github/workflows/gates.yml")
    bare = git.commit_all(forge_repo, "Unpublish")
    project = _referencing_project(tmp_path / "bare", git, hooks_rev=bare, ci_rev=bare)
    failing = _evaluate(project, forge_repo)
    assert failing["forge-hooks"] == f"forge commit {bare} publishes no .pre-commit-hooks.yaml"
    assert failing["base-hooks"] == "needs one valid forge pin (see forge-hooks)"


def test_ci_needs_the_workflow_at_the_pinned_commit(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    git(forge_repo, "rm", "-q", ".github/workflows/gates.yml")
    revision = git.commit_all(forge_repo, "Drop the workflow")
    project = _referencing_project(tmp_path, git, hooks_rev=revision, ci_rev=revision)
    assert _evaluate(project, forge_repo) == {
        "ci": f"forge commit {revision} has no reusable .github/workflows/gates.yml"
    }


@pytest.mark.parametrize(
    "config",
    [
        "repos: 1\n",
        "default_stages: true\nrepos: []\n",
        "repos:\n  - repo: local\n    hooks: 1\n",
        "repos:\n  - repo: local\n    hooks:\n      - id: x\n        stages: 1\n",
    ],
)
def test_malformed_precommit_config_fails_instead_of_crashing(
    forge_repo: Path, tmp_path: Path, git: Git, config: str
) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    _set_precommit(project, config)
    assert _evaluate(project, forge_repo)["forge-hooks"] == "no valid .pre-commit-config.yaml"


def test_an_unquoted_numeric_rev_is_not_a_commit_sha(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    _set_precommit(
        project,
        "default_stages: [pre-commit]\nrepos:"
        + _forge_entry("1" * 40).replace(f"rev: {'1' * 40}", f"rev: {1 * 10**39}")
        + "\n",
    )
    assert _evaluate(project, forge_repo)["forge-hooks"] == f"rev {10**39} is not a commit SHA"


def test_owner_and_repository_names_match_case_insensitively(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    config = project / ".pre-commit-config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(f"repo: {FORGE_URL}", "repo: git@github.com:Owner/Forge.git"),
        encoding="utf-8",
    )
    workflow = project / ".github" / "workflows" / "ci.yml"
    workflow.write_text(workflow.read_text(encoding="utf-8").replace("owner/forge/", "Owner/Forge/"), encoding="utf-8")
    agents = project / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8").replace("owner/forge", "OWNER/forge"), encoding="utf-8")
    assert _evaluate(project, forge_repo) == {}


def test_only_yml_and_yaml_files_count_as_workflows(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    workflows = project / ".github" / "workflows"
    (workflows / "ci.yml").rename(workflows / "ci.yignoredml")
    assert _evaluate(project, forge_repo) == {"ci": "no job uses owner/forge/.github/workflows/gates.yml"}


@pytest.mark.parametrize(
    ("form", "passes"),
    [
        ("- [{name}]({url})", True),
        ("- <{url}>", True),
        ("- [{name}][ref-{n}]\n\n[ref-{n}]: {url}", True),
        ("<!-- [{name}]({url}) -->", False),
        ("```md\n[{name}]({url})\n```", False),
        ("See {url} for the rules.", False),
    ],
)
def test_rule_links_count_only_as_visible_markdown_links(
    forge_repo: Path, tmp_path: Path, git: Git, form: str, passes: bool
) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    lines = [
        form.format(name=name, n=n, url=f"{FORGE_URL}/blob/{head}/AGENTS.md#{rules.section_anchor(name)}")
        for n, name in enumerate(TRAVELLING)
    ]
    (project / "AGENTS.md").write_text("# Referencing\n\n## Rules\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    assert ("agents-sections" not in _evaluate(project, forge_repo)) is passes


def test_rule_links_leave_fenced_examples_and_a_project_section_of_that_name(forge_repo: Path) -> None:
    forge_sections = rules.sections((forge_repo / "AGENTS.md").read_text(encoding="utf-8"))
    verifying = forge_sections["Verifying"]
    own = "## Working rules from forge\n\nOur own notes.\n"
    example = f"## Examples\n\n```md\n{verifying}```\n"
    text = f"# Mine\n\n{example}\n{own}\n{verifying.rstrip()}"
    linked = rules.apply_rule_links(text, "owner/forge", "a" * 40, TRAVELLING, forge_sections)
    assert example in linked and own in linked
    assert linked.count("A task is done when the gates pass.") == 1, "the copy at EOF without a newline is gone"
    assert linked.endswith(_links("a" * 40))


def test_a_longer_fence_holds_shorter_fence_lines_and_headings(forge_repo: Path) -> None:
    forge_sections = rules.sections((forge_repo / "AGENTS.md").read_text(encoding="utf-8"))
    example = "## Examples\n\n````md\n```\n## Verifying\n\nOurs.\n```\n````\n"
    linked = rules.apply_rule_links(f"# Mine\n\n{example}", "owner/forge", "a" * 40, TRAVELLING, forge_sections)
    assert linked.startswith(f"# Mine\n\n{example}")


def test_a_projects_own_section_under_a_rule_heading_is_kept_and_reported(
    forge_repo: Path, tmp_path: Path, git: Git
) -> None:
    forge_sections = rules.sections((forge_repo / "AGENTS.md").read_text(encoding="utf-8"))
    own = "## Verifying\n\nRun the oracle captures on Windows too.\n"
    linked = rules.apply_rule_links(f"# Mine\n\n{own}", "owner/forge", "a" * 40, TRAVELLING, forge_sections)
    assert own in linked
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    agents = project / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "\n" + own, encoding="utf-8")
    assert _evaluate(project, forge_repo)["agents-sections"] == "has its own sections named like forge's: Verifying"


def test_legacy_stage_names_and_stage_order_resolve_like_pre_commit(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    _set_precommit(
        project, "default_stages: [commit]\nrepos:" + _forge_entry(head, push_stages="\n        stages: [push]") + "\n"
    )
    assert "base-hooks" not in _evaluate(project, forge_repo)


def test_an_annotated_tag_sha_is_not_a_commit_pin(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    git(forge_repo, "tag", "-a", "v1", "-m", "v1")
    tag = git(forge_repo, "rev-parse", "v1")
    project = _referencing_project(tmp_path, git, hooks_rev=tag, ci_rev=tag)
    assert (
        _evaluate(project, forge_repo)["forge-hooks"] == f"rev {tag} is not a commit in forge's checkout (fetch forge)"
    )


def test_rule_links_must_name_a_forge_commit_with_those_sections(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head, links_rev="0" * 40)
    assert _evaluate(project, forge_repo)["agents-sections"] == (
        "no pinned link to: A note from the maintainer, Verifying, Commits, Taste"
    )


@pytest.mark.parametrize(
    ("workflow", "as_directory"),
    [("on:\n  push:\njobs: {}\n", False), ("", True)],
)
def test_ci_needs_a_reusable_workflow_file_at_the_pin(
    forge_repo: Path, tmp_path: Path, git: Git, workflow: str, as_directory: bool
) -> None:
    gates = forge_repo / ".github" / "workflows" / "gates.yml"
    if as_directory:
        git(forge_repo, "rm", "-q", str(gates))
        (gates / "README.md").parent.mkdir(parents=True)
        (gates / "README.md").write_text("not a workflow\n", encoding="utf-8")
    else:
        gates.write_text(workflow, encoding="utf-8")
    revision = git.commit_all(forge_repo, "Break the workflow")
    project = _referencing_project(tmp_path, git, hooks_rev=revision, ci_rev=revision)
    assert (
        _evaluate(project, forge_repo)["ci"] == f"forge commit {revision} has no reusable .github/workflows/gates.yml"
    )


def test_a_forge_entry_without_hooks_is_invalid_config(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    config = project / ".pre-commit-config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + f"  - repo: {FORGE_URL}\n    rev: {head}\n", encoding="utf-8"
    )
    assert _evaluate(project, forge_repo)["forge-hooks"] == "no valid .pre-commit-config.yaml"


@pytest.mark.parametrize(
    "form",
    ["- `[{name}]({url})`", "[ref-{n}]: {url}"],
)
def test_links_in_inline_code_and_unused_references_do_not_count(
    forge_repo: Path, tmp_path: Path, git: Git, form: str
) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    lines = [
        form.format(name=name, n=n, url=f"{FORGE_URL}/blob/{head}/AGENTS.md#{rules.section_anchor(name)}")
        for n, name in enumerate(TRAVELLING)
    ]
    (project / "AGENTS.md").write_text("# Referencing\n\n## Rules\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    assert "agents-sections" in _evaluate(project, forge_repo)


def test_a_directory_named_like_a_workflow_is_skipped(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    (project / ".github" / "workflows" / "examples.yaml").mkdir()
    assert _evaluate(project, forge_repo) == {}


def test_a_fence_closer_followed_by_a_non_breaking_space_does_not_close(forge_repo: Path) -> None:
    forge_sections = rules.sections((forge_repo / "AGENTS.md").read_text(encoding="utf-8"))
    example = "## Examples\n\n```\n```\u00a0\n## Verifying\n\n- A task is done when the gates pass.\n```\n"
    linked = rules.apply_rule_links(f"# Mine\n\n{example}", "owner/forge", "a" * 40, TRAVELLING, forge_sections)
    assert linked.startswith(f"# Mine\n\n{example}")


def test_a_copy_from_the_recorded_forge_revision_is_also_removed(forge_repo: Path) -> None:
    old = rules.sections((forge_repo / "AGENTS.md").read_text(encoding="utf-8"))
    new = {**old, "Taste": "## Taste\n\n- Smaller diffs.\n"}
    linked = rules.apply_rule_links(f"# Mine\n\n{old['Taste']}", "owner/forge", "a" * 40, TRAVELLING, new, old)
    assert "Guard clauses first." not in linked


def test_links_need_a_forge_agents_md_file_at_the_commit(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    git(forge_repo, "rm", "-q", "AGENTS.md")
    (forge_repo / "AGENTS.md").mkdir()
    for name in TRAVELLING:
        (forge_repo / "AGENTS.md" / f"## {name}").write_text("x\n", encoding="utf-8")
    odd = git.commit_all(forge_repo, "AGENTS.md as a directory")
    git(forge_repo, "rm", "-rq", "AGENTS.md")
    git(forge_repo, "checkout", "HEAD~1", "--", "AGENTS.md")
    head = git.commit_all(forge_repo, "Restore AGENTS.md")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head, links_rev=odd)
    assert "agents-sections" in _evaluate(project, forge_repo)


def test_a_workflow_needs_workflow_call_under_a_literal_on_key(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    (forge_repo / ".github" / "workflows" / "gates.yml").write_text(
        "yes:\n  workflow_call:\njobs: {}\n", encoding="utf-8"
    )
    revision = git.commit_all(forge_repo, "Not a reusable workflow")
    project = _referencing_project(tmp_path, git, hooks_rev=revision, ci_rev=revision)
    assert (
        _evaluate(project, forge_repo)["ci"] == f"forge commit {revision} has no reusable .github/workflows/gates.yml"
    )


def test_a_link_to_a_commit_with_undecodable_agents_md_is_invalid(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    good = git(forge_repo, "rev-parse", "HEAD")
    (forge_repo / "AGENTS.md").write_bytes(b"## Verifying\n\xff\xfe\n")
    bad = git.commit_all(forge_repo, "Latin-1 bytes")
    git(forge_repo, "checkout", good, "--", "AGENTS.md")
    project = _referencing_project(tmp_path, git, hooks_rev=good, ci_rev=good, links_rev=bad)
    assert "agents-sections" in _evaluate(project, forge_repo)


@pytest.mark.parametrize("form", ["- ``[{name}]({url})``", "![ref-{n}]\n\n[ref-{n}]: {url}"])
def test_double_backtick_code_and_image_references_are_not_links(
    forge_repo: Path, tmp_path: Path, git: Git, form: str
) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    lines = [
        form.format(name=name, n=n, url=f"{FORGE_URL}/blob/{head}/AGENTS.md#{rules.section_anchor(name)}")
        for n, name in enumerate(TRAVELLING)
    ]
    (project / "AGENTS.md").write_text("# Referencing\n\n## Rules\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    assert "agents-sections" in _evaluate(project, forge_repo)


def test_a_section_missing_at_the_target_revision_is_neither_removed_nor_linked(forge_repo: Path) -> None:
    old = rules.sections((forge_repo / "AGENTS.md").read_text(encoding="utf-8"))
    target = {name: text for name, text in old.items() if name != "Verifying"}
    linked = rules.apply_rule_links(f"# Mine\n\n{old['Verifying']}", "owner/forge", "a" * 40, TRAVELLING, target, old)
    assert old["Verifying"] in linked and "#verifying" not in linked


def test_rule_checks_fail_when_forges_own_agents_md_cannot_be_read(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    (forge_repo / "AGENTS.md").unlink()
    (forge_repo / "AGENTS.md").mkdir()
    assert _evaluate(project, forge_repo)["agents-sections"] == "forge's AGENTS.md cannot be read"


@pytest.mark.parametrize("which", ["forge", "project"])
def test_an_unreadable_agents_md_is_reported_not_raised(forge_repo: Path, tmp_path: Path, git: Git, which: str) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    path = (forge_repo if which == "forge" else project) / "AGENTS.md"
    path.chmod(0)
    try:
        failing = _evaluate(project, forge_repo)
    finally:
        path.chmod(0o644)
    expected = "forge's AGENTS.md cannot be read" if which == "forge" else "no AGENTS.md"
    assert failing["agents-sections"] == expected


def test_only_commonmark_line_endings_and_top_level_headings_split_sections(forge_repo: Path) -> None:
    forge_sections = rules.sections((forge_repo / "AGENTS.md").read_text(encoding="utf-8"))
    link = f"- [Taste]({FORGE_URL}/blob/{'a' * 40}/AGENTS.md#taste)"
    own = (
        "# Mine\n\nOne rule\u2028on two visual lines.\n\n"
        f"> ## {rules.RULES_SECTION}\n\n{link}\n\n## Own\n\nKeep me.\n\n"
    )
    linked = rules.apply_rule_links(
        own + forge_sections["Verifying"], "owner/forge", "b" * 40, TRAVELLING, forge_sections
    )
    assert linked.startswith(own) and forge_sections["Verifying"] not in linked
    assert rules.sections(own + forge_sections["Verifying"]).keys() == {"Own", "Verifying"}


def test_a_generated_link_to_an_accented_section_is_accepted(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    agents = forge_repo / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "\n## Vérification\n\nCheck it.\n", encoding="utf-8")
    head = git.commit_all(forge_repo, "Add an accented section")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    travelling = [*TRAVELLING, "Vérification"]
    (project / "AGENTS.md").write_text(
        f"# Referencing\n\n{rules.rule_links('owner/forge', head, travelling)}", encoding="utf-8"
    )
    report = rules.evaluate(project, ForgeSource(forge_repo), travelling_sections=travelling, forge_head=head)
    assert next(check for check in report.checks if check.id == "agents-sections").result == "pass"


@pytest.mark.parametrize(
    ("text", "counted"),
    [
        ("Press the ` key.\n\n{links}", True),
        ("``` not a fence `` still prose\n\n{links}", True),
        ("{images}", False),
    ],
)
def test_links_are_what_commonmark_renders(
    forge_repo: Path, tmp_path: Path, git: Git, text: str, counted: bool
) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    project = _referencing_project(tmp_path, git, hooks_rev=head, ci_rev=head)
    urls = [f"{FORGE_URL}/blob/{head}/AGENTS.md#{rules.section_anchor(name)}" for name in TRAVELLING]
    links = "\n".join(f"- [{name}]({url})" for name, url in zip(TRAVELLING, urls, strict=True))
    images = (
        "\n".join(f"![See [forge][rule-{n}]][img]\n\n[rule-{n}]: {url}" for n, url in enumerate(urls))
        + "\n\n[img]: https://example.org/i.png"
    )
    body = text.format(links=links, images=images)
    (project / "AGENTS.md").write_text(f"# Referencing\n\n## Rules\n\n{body}\n", encoding="utf-8")
    assert ("agents-sections" not in _evaluate(project, forge_repo)) is counted


def test_rule_links_replace_copied_sections_and_follow_the_revision(forge_repo: Path) -> None:
    forge_agents = (forge_repo / "AGENTS.md").read_text(encoding="utf-8")
    copied = (
        "# Mine\n\n## Intro\n\nHi.\n\n" + rules.sections(forge_agents)["Verifying"] + "\n## Project notes\n\nOurs.\n"
    )
    forge_sections = rules.sections(forge_agents)
    linked = rules.apply_rule_links(copied, "owner/forge", "a" * 40, TRAVELLING, forge_sections)
    assert "Hi." in linked and "Ours." in linked and "A task is done when the gates pass." not in linked
    assert linked.endswith(_links("a" * 40))
    moved = rules.apply_rule_links(linked, "owner/forge", "b" * 40, TRAVELLING, forge_sections)
    assert moved.endswith(_links("b" * 40)) and moved.count("## Working rules") == 1 and "Ours." in moved


def test_template_revision_is_written_without_touching_the_rest() -> None:
    manifest = MANIFEST.replace("{name}", "X").replace("{project_id}", "x")
    updated = set_template_revision(manifest, "abc123", datetime(2026, 11, 5, 12, tzinfo=UTC))
    assert updated.startswith(manifest.rstrip("\n").lstrip("\n")[:20]) or manifest.strip() in updated
    data = tomllib.loads(updated)
    assert data["forge"]["template_revision"] == "abc123"
    again = set_template_revision(updated, "def456", datetime(2026, 11, 6, tzinfo=UTC))
    assert tomllib.loads(again)["forge"]["template_revision"] == "def456" and again.count("[forge]") == 1


def test_the_brief_carries_failing_checks_rules_and_forge_text(forge_repo: Path, tmp_path: Path, git: Git) -> None:
    head = git(forge_repo, "rev-parse", "HEAD")
    live = forge_repo / "AGENTS.md"
    live.write_text(live.read_text(encoding="utf-8").replace("Guard clauses first.", "Live edit."), encoding="utf-8")
    report = rules.evaluate(tmp_path, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="abc")
    brief = standards_brief(
        project_name="Demo",
        languages=["python"],
        failing=report.failing,
        forge=ForgeSource(forge_repo),
        travelling=TRAVELLING,
        slug="owner/forge",
        revision=head,
    )
    assert brief.startswith("# Bring up to standards")
    assert "<<<FORGE_AGENTS_MD" in brief and "Guard clauses first." in brief and "Live edit." not in brief
    assert f'template_revision = "{head}"' in brief and "- forge-hooks:" in brief
    assert f"owner/forge/.github/workflows/gates.yml@{head}" in brief and "<<<FORGE_HOOKS" in brief


NONCOMPLIANT = {
    "README.md": "# Messy\n\nNo instructions.\n",
    ".gitignore": "__pycache__/\n",
    "app.py": "print('hi')\n",
}


@pytest.fixture
async def standards_env(
    data_paths: DataPaths, tmp_path: Path, forge_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService]]:
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "pre-commit-home"))
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git_service = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git_service, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    standards = StandardsService(runtime, projects, jobs, checks, agents)
    runtime.register([jobs, scheduler, manifests, projects, git_service, checks, agents, standards, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")], "forge_repo": str(forge_repo)})
    await runtime.db.kv_put("config", "agents", AgentsConfig(harness=fake_harness_config()).model_dump(mode="json"))
    await runtime.start()
    try:
        yield standards, projects, discovery, jobs, agents
    finally:
        await runtime.stop()


async def test_bring_up_fixes_deterministically_then_with_the_agent_on_a_branch(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    make_project: Callable[..., Path],
    git: Git,
    forge_repo: Path,
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    messy = make_project("Messy", extra=NONCOMPLIANT)
    await discovery.add_project(PathInput(path=str(messy)))
    await wait_for(lambda: (facts := projects.summary("messy").standards) is not None and facts.total > 0)
    before = projects.summary("messy").standards
    assert before is not None and before.passing < before.total and before.drift

    final = await jobs.wait((await standards.bring_up(ProjectRef(project_id="messy"))).job_id)
    assert final.status is JobStatus.ATTENTION and final.summary is not None and "->" in final.summary
    [run] = (await agents.branch_runs(BranchRunsInput(project_id="messy", kind="standards"))).runs
    assert run.state is BranchRunState.READY and run.branch.startswith("forge-standards/")
    assert run.hooks_installed is True
    for kind in ("pre-commit", "pre-push"):
        hook = messy / ".git" / "hooks" / kind
        assert hook.is_file(), "hooks are installed in the main repo"
        assert os.access(hook, os.X_OK), "installed hooks must be executable"
    assert not (messy / "CLAUDE.md").exists(), "the user's working tree is untouched until merge"

    worktree = Path(run.worktree)
    after = rules.evaluate(worktree, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head="x")
    passing = {check.id for check in after.checks if check.result == "pass"}
    assert {"manifest", "agents-md", "agents-sections", "claude-md", "readme", "gitignore"} <= passing
    assert not (worktree / "tools").exists(), "bring-up references forge instead of copying it"
    head = git(forge_repo, "rev-parse", "HEAD")
    assert after.template_revision == head

    await agents.merge_branch(BranchRunInput(run_id=run.id))
    assert (messy / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    standing = await standards.evaluate(ProjectRef(project_id="messy"))
    assert standing.report is not None and standing.behind == 0 and standing.report.template_revision == head
    facts = projects.summary("messy").standards
    assert facts is not None and facts.passing > before.passing and facts.forge_commits_behind == 0


@pytest.mark.parametrize(
    ("unpublish", "message"),
    [
        (("remote", "remove", "origin"), "no GitHub origin remote"),
        (("rm", "-q", ".pre-commit-hooks.yaml"), "publishes no .pre-commit-hooks.yaml"),
    ],
)
async def test_bring_up_is_refused_while_forge_cannot_be_referenced(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    make_project: Callable[..., Path],
    git: Git,
    forge_repo: Path,
    unpublish: tuple[str, ...],
    message: str,
) -> None:
    standards, _projects, discovery, _jobs, agents = standards_env
    git(forge_repo, *unpublish)
    if unpublish[0] == "rm":
        git.commit_all(forge_repo, "Unpublish")
    await discovery.add_project(PathInput(path=str(make_project("Waiting"))))
    with pytest.raises(ActionError, match=message):
        await standards.bring_up(ProjectRef(project_id="waiting"))
    assert (await agents.branch_runs(BranchRunsInput(project_id="waiting", kind="standards"))).runs == []


async def test_deterministic_fixes_skip_a_bad_recorded_revision_and_missing_target_sections(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    forge_repo: Path,
    tmp_path: Path,
    git: Git,
) -> None:
    standards = standards_env[0]
    forge_sections = rules.sections((forge_repo / "AGENTS.md").read_text(encoding="utf-8"))
    project = tmp_path / "aligned"
    manifest = MANIFEST.replace("{name}", "Aligned").replace("{project_id}", "aligned")
    write_files(project, {"forge.project.toml": manifest + '\n[forge]\ntemplate_revision = "a\\u0000b"\n'})
    (project / "AGENTS.md").write_text(f"# Aligned\n\n{forge_sections['Verifying']}", encoding="utf-8")
    without = forge_repo / "AGENTS.md"
    without.write_text(without.read_text(encoding="utf-8").replace("## Verifying", "## Checking"), encoding="utf-8")
    revision = git.commit_all(forge_repo, "Rename a section")
    changed = await standards.apply_deterministic(project, ForgeSource(forge_repo), revision)
    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "AGENTS.md (links to forge's working rules)" in changed
    assert forge_sections["Verifying"] in agents and "#verifying" not in agents


async def test_forge_commits_since_the_revision_are_listed(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    make_project: Callable[..., Path],
    git: Git,
    forge_repo: Path,
) -> None:
    standards, _projects, discovery, _jobs, _agents = standards_env
    revision = git(forge_repo, "rev-parse", "HEAD")
    manifest = MANIFEST.replace("{name}", "Aligned").replace("{project_id}", "aligned")
    manifest += f'\n[forge]\ntemplate_revision = "{revision}"\n'
    aligned = make_project("Aligned", manifest=manifest)
    (forge_repo / "NEWS.md").write_text("news\n", encoding="utf-8")
    git.commit_all(forge_repo, "Tighten a gate")
    await discovery.add_project(PathInput(path=str(aligned)))
    standing = await standards.evaluate(ProjectRef(project_id="aligned"))
    assert standing.behind == 1 and [c.subject for c in standing.forge_commits_since] == ["Tighten a gate"]
    report = await standards.report(ProjectRef(project_id="aligned"))
    assert report.report is not None and report.report.forge_head == git(forge_repo, "rev-parse", "HEAD")


async def test_evaluation_ignores_an_unusable_recorded_revision(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    make_project: Callable[..., Path],
) -> None:
    standards, _projects, discovery, _jobs, _agents = standards_env
    manifest = MANIFEST.replace("{name}", "Odd").replace("{project_id}", "odd")
    odd = make_project("Odd", manifest=manifest + '\n[forge]\ntemplate_revision = "a\\u0000b"\n')
    await discovery.add_project(PathInput(path=str(odd)))
    standing = await standards.evaluate(ProjectRef(project_id="odd"))
    assert standing.behind is None and standing.forge_commits_since == []


async def test_adopt_writes_a_valid_manifest_with_a_repair_loop(
    standards_env: tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService],
    make_project: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    loose = make_project("Loose", manifest=None)
    await discovery.add_project(PathInput(path=str(loose)))
    project_id = next(record.id for record in projects.records() if record.path == loose.resolve())
    monkeypatch.setenv("FAKE_HARNESS_SABOTAGE_FIRST", "1")
    final = await jobs.wait((await standards.adopt(ProjectRef(project_id=project_id))).job_id)
    assert final.status is JobStatus.ATTENTION
    [run] = (await agents.branch_runs(BranchRunsInput(project_id=project_id, kind="adopt"))).runs
    assert run.agent_attempts == 2 and run.state is BranchRunState.READY
    await agents.merge_branch(BranchRunInput(run_id=run.id))
    adopted = tomllib.loads((loose / "forge.project.toml").read_text(encoding="utf-8"))
    assert adopted["requirements"] == {}
    await projects.refresh(project_id)
    with pytest.raises(ActionError, match="already has"):
        await standards.adopt(ProjectRef(project_id=project_id))


StandardsEnv = tuple[StandardsService, ProjectsService, DiscoveryService, JobsService, AgentsService]


def _id_of(projects: ProjectsService, path: Path) -> str:
    return next(record.id for record in projects.records() if record.path == path.resolve())


async def test_adopt_stops_at_a_harness_that_did_not_run_instead_of_retrying_it(
    standards_env: StandardsEnv, make_project: Callable[..., Path]
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    loose = make_project("Loose", manifest=None)
    await discovery.add_project(PathInput(path=str(loose)))
    project_id = _id_of(projects, loose)
    await agents.update_config(AgentsConfig(harness=HarnessConfig(command=["definitely-not-a-harness-xyz"])))

    final = await jobs.wait((await standards.adopt(ProjectRef(project_id=project_id))).job_id)

    assert final.status is JobStatus.FAILED and final.summary == "agent harness-missing"
    [run] = (await agents.branch_runs(BranchRunsInput(project_id=project_id, kind="adopt"))).runs
    assert run.state is BranchRunState.FAILED and run.error == "harness-missing" and run.agent_attempts == 1
    assert len(run.agent_run_ids) == 1


async def test_a_failure_while_adopting_fails_the_run_instead_of_leaving_it_running(
    standards_env: StandardsEnv, make_project: Callable[..., Path]
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    loose = make_project("Loose", manifest=None)
    await discovery.add_project(PathInput(path=str(loose)))
    project_id = _id_of(projects, loose)
    hook = loose / ".git" / "hooks" / "pre-commit"
    write_hook(hook, "#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")

    final = await jobs.wait((await standards.adopt(ProjectRef(project_id=project_id))).job_id)

    assert final.status is JobStatus.FAILED and "hook says no" in (final.error or "")
    [run] = (await agents.branch_runs(BranchRunsInput(project_id=project_id, kind="adopt"))).runs
    assert run.state is BranchRunState.FAILED and "hook says no" in (run.error or "")
    discarded = await agents.discard_branch(BranchRunInput(run_id=run.id))
    assert discarded.state is BranchRunState.DISCARDED and not Path(run.worktree).exists()


async def test_adopt_starts_from_the_repos_own_default_branch(
    standards_env: StandardsEnv, make_project: Callable[..., Path], git: Git
) -> None:
    standards, projects, discovery, jobs, agents = standards_env
    loose = make_project("Loose", manifest=None)
    git(loose, "branch", "-m", "main", "trunk")
    await discovery.add_project(PathInput(path=str(loose)))
    project_id = _id_of(projects, loose)

    final = await jobs.wait((await standards.adopt(ProjectRef(project_id=project_id))).job_id)

    assert final.status is JobStatus.ATTENTION
    [run] = (await agents.branch_runs(BranchRunsInput(project_id=project_id, kind="adopt"))).runs
    assert run.base_branch == "trunk" and run.state is BranchRunState.READY


async def test_without_the_forge_repo_bring_up_is_refused(
    data_paths: DataPaths, tmp_path: Path, make_project: Callable[..., Path]
) -> None:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git_service = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git_service, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    standards = StandardsService(runtime, projects, jobs, checks, agents)
    runtime.register([jobs, scheduler, manifests, projects, git_service, checks, agents, standards, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")]})
    await runtime.start()
    try:
        await discovery.add_project(PathInput(path=str(make_project("Alone"))))
        with pytest.raises(ActionError, match="forge repo was not found"):
            await standards.bring_up(ProjectRef(project_id="alone"))
        standing = await standards.evaluate(ProjectRef(project_id="alone"))
        assert standing.error == "the forge repo was not found" and standing.report is None
    finally:
        await runtime.stop()


LIVE_AGENT = shutil.which("claude") is not None and os.environ.get("FORGE_DASHBOARD_LIVE_AGENT") == "1"


@pytest.mark.live
@pytest.mark.skipif(not LIVE_AGENT, reason="needs the claude CLI and FORGE_DASHBOARD_LIVE_AGENT=1 (spends tokens)")
async def test_live_claude_brings_a_messy_repo_up_to_standards(
    data_paths: DataPaths,
    tmp_path: Path,
    forge_repo: Path,
    make_project: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRE_COMMIT_HOME", str(tmp_path / "pre-commit-home"))
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git_service = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git_service, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    standards = StandardsService(runtime, projects, jobs, checks, agents)
    runtime.register([jobs, scheduler, manifests, projects, git_service, checks, agents, standards, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")], "forge_repo": str(forge_repo)})
    config = AgentsConfig()
    config.kinds.standards.model = "haiku"
    config.kinds.standards.max_turns = 40
    await runtime.db.kv_put("config", "agents", config.model_dump(mode="json"))
    await runtime.start()
    try:
        messy = make_project("Messy", extra=NONCOMPLIANT)
        await discovery.add_project(PathInput(path=str(messy)))
        final = await jobs.wait((await standards.bring_up(ProjectRef(project_id="messy"))).job_id)
        [run] = (await agents.branch_runs(BranchRunsInput(project_id="messy", kind="standards"))).runs
        assert final.status is JobStatus.ATTENTION, f"{final.summary} {run.error}"
        assert run.compliance_before is not None and run.compliance_after is not None
        assert run.compliance_after.passing > run.compliance_before.passing
        worktree = Path(run.worktree)
        assert (worktree / "AGENTS.md").is_file()
        report = rules.evaluate(worktree, ForgeSource(forge_repo), travelling_sections=TRAVELLING, forge_head=None)
        assert _results(report)["agents-sections"] == "pass"
    finally:
        await runtime.stop()
