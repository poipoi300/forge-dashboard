"""The published schema and the contract document stay in step with the models."""

import json
import re
import tomllib
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from forge_dashboard.manifest import models
from forge_dashboard.manifest.schema import manifest_json_schema, manifest_json_schema_text

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = REPO_ROOT / "schema" / "forge.project.schema.json"
DOC = (REPO_ROOT / "docs" / "project-manifest.md").read_text(encoding="utf-8")


def test_committed_schema_matches_models() -> None:
    assert SCHEMA_FILE.read_text(encoding="utf-8") == manifest_json_schema_text(), (
        "schema drifted: run `uv run forge-dashboard manifest schema > schema/forge.project.schema.json`"
    )


def test_schema_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(manifest_json_schema())


def test_schema_accepts_the_documented_example_and_rejects_unknown_keys() -> None:
    example = re.search(r"## Example\n\n```toml\n(?P<body>.*?)```", DOC, flags=re.DOTALL)
    assert example is not None
    document = tomllib.loads(example["body"])
    validator = Draft202012Validator(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))
    assert list(validator.iter_errors(document)) == []
    assert list(validator.iter_errors({**document, "extra": 1}))


def _documented_keys(heading: str) -> set[str]:
    """Keys in the first `| Key |` table under the given `## ` heading."""
    section = DOC.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    table = section.split("| Key |", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", table, flags=re.MULTILINE))


def _model_keys(model: type[BaseModel]) -> set[str]:
    return {field.alias or name for name, field in model.model_fields.items()}


def test_every_documented_table_matches_its_model() -> None:
    tables: dict[str, tuple[type[BaseModel], set[str]]] = {
        "`[project]` (required)": (models.Project, set()),
        "`[v1]` (required)": (models.V1, {"milestones"}),
        "`[requirements]` (optional)": (models.HostRequirements, set()),
        "`[[checks]]` (0..n)": (models.Check, set()),
        "`[[services]]` (0..n)": (models.Service, set()),
        "`[[deployments]]` (0..n)": (models.Deployment, set()),
        "`[[metrics]]` (0..n)": (models.Metric, set()),
        "`[[actions]]` (0..n)": (models.Action, set()),
        "`[launch]` (optional)": (models.Launch, set()),
        "`[forge]` (optional, written by tools)": (models.ForgeAlignment, set()),
        "Top level": (
            models.ProjectManifest,
            {
                "project",
                "v1",
                "requirements",
                "checks",
                "services",
                "deployments",
                "metrics",
                "actions",
                "launch",
                "forge",
            },
        ),
    }
    for heading, (model, undocumented_in_table) in tables.items():
        assert _documented_keys(heading) | undocumented_in_table == _model_keys(model), heading


def test_milestone_table_matches_its_model() -> None:
    section = DOC.split('With `source = "milestones"`', 1)[1]
    table = section.split("| Key |", 1)[1].split("\n\n", 1)[0]
    assert set(re.findall(r"^\| `([a-z_]+)` \|", table, flags=re.MULTILINE)) == _model_keys(models.Milestone)
