"""The gate registry must stay in sync with the actual hook/CI configs."""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge_dashboard.gates import DETERMINISTIC_GATES, GateStage, gates_for_stage

REPO_ROOT = Path(__file__).resolve().parent.parent


def _precommit_hooks() -> list[dict[str, Any]]:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    return [hook for repo in config["repos"] for hook in repo["hooks"]]


def test_registry_matches_precommit_config() -> None:
    registry_ids = {g.name for g in DETERMINISTIC_GATES}
    assert registry_ids == {hook["id"] for hook in _precommit_hooks()}


def test_registry_stages_match_precommit_config() -> None:
    config_stages = {hook["id"]: tuple(hook.get("stages", ["pre-commit"])) for hook in _precommit_hooks()}
    assert {g.name: (g.stage.value,) for g in DETERMINISTIC_GATES} == config_stages


def test_every_hook_is_a_local_system_hook() -> None:
    assert {hook.get("language") for hook in _precommit_hooks()} == {"system"}


def test_push_stage_holds_only_slow_gates() -> None:
    assert {g.name for g in gates_for_stage(GateStage.PUSH)} == {"tests-and-diff-coverage"}


@pytest.mark.skipif(os.name == "nt", reason="exercises the Bash gate with a POSIX uv stub")
def test_push_gate_runs_python_tests_without_git_hook_context(tmp_path: Path) -> None:
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "${GIT_DIR-unset}" "${GIT_INDEX_FILE-unset}" "$@"\nexit 79\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "tools" / "stack_coverage_gate.sh")],
        cwd=REPO_ROOT,
        env=os.environ
        | {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "GIT_DIR": str(tmp_path / "hook-git-dir"),
            "GIT_INDEX_FILE": str(tmp_path / "hook-index"),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 79, result.stderr
    assert result.stdout.splitlines() == ["unset", "unset", "run", "coverage", "run", "-m", "pytest"]


def test_ci_enforced_gates_appear_in_workflow() -> None:
    workflow_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # CI runs the tools directly rather than via pre-commit; map hook id -> the
    # command substring that proves the gate is present in the workflow.
    expected_evidence = {
        "ruff-format": "ruff format --check",
        "ruff-check": "ruff check",
        "mypy": "mypy .",
        "biome": "biome check .",
        "tsc": "tsc --noEmit",
        "tests-and-diff-coverage": "stack_coverage_gate.sh",
    }
    ci_gates = {g.name for g in DETERMINISTIC_GATES if g.ci_enforced}
    assert ci_gates == set(expected_evidence)
    for evidence in expected_evidence.values():
        assert evidence in workflow_text
