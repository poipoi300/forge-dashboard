"""JSON Schema for `forge.project.toml`, generated from the pydantic models."""

from __future__ import annotations

import json
from typing import Any

from forge_dashboard.manifest.models import ProjectManifest

SCHEMA_ID = "https://forge.local/schema/forge.project.schema.json"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def manifest_json_schema() -> dict[str, Any]:
    """Return the manifest schema as a JSON-compatible dict."""
    schema = ProjectManifest.model_json_schema(by_alias=True, mode="validation")
    return {"$schema": SCHEMA_DIALECT, "$id": SCHEMA_ID, **schema}


def manifest_json_schema_text() -> str:
    """Return the schema exactly as committed to `schema/forge.project.schema.json`."""
    return json.dumps(manifest_json_schema(), indent=2, sort_keys=True) + "\n"
