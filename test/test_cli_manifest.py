"""`forge-dashboard manifest validate|schema`."""

import json
from pathlib import Path

import pytest

from forge_dashboard.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_validate_reports_ok_for_a_valid_repo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["manifest", "validate", str(REPO_ROOT)]) == 0
    assert capsys.readouterr().out.strip().endswith(": ok")


def test_validate_prints_line_numbers_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "forge.project.toml"
    manifest.write_text("schema = 1\n\n[project]\nname = 'x'\n", encoding="utf-8")
    assert main(["manifest", "validate", str(manifest)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert f"{manifest}:3: project.id: Field required" in out
    assert any(line.startswith(f"{manifest}:") and " v1: " in line for line in out)


def test_schema_prints_the_json_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["manifest", "schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "forge.project.toml"
    assert "project" in schema["required"]
