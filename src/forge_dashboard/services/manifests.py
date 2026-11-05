"""Load and validate every project's `forge.project.toml`, keeping exact errors."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from forge_dashboard.core.service import NoInput, Service, action
from forge_dashboard.core.ui import ui
from forge_dashboard.manifest import ManifestIssue, ManifestLoad, load_manifest, parse_manifest_text
from forge_dashboard.manifest.schema import manifest_json_schema


class ManifestStatus(BaseModel):
    project_id: str
    path: str = Field(json_schema_extra=ui(format="path"))
    valid: bool = Field(json_schema_extra=ui(format="badge", tones={"true": "good", "false": "bad"}))
    issues: list[ManifestIssue] = Field(
        default_factory=list, json_schema_extra=ui(columns=["line", "location", "message"])
    )


class ManifestsConfig(BaseModel):
    """No settings: manifests are read from disk on every change."""


class ManifestsState(BaseModel):
    manifests: list[ManifestStatus] = Field(
        default_factory=list, json_schema_extra=ui(columns=["project_id", "valid", "path"])
    )


class ValidatePathInput(BaseModel):
    path: str = Field(json_schema_extra=ui(widget="path-file", help="A forge.project.toml or the repo containing it."))


class ValidateTextInput(BaseModel):
    text: str = Field(json_schema_extra=ui(widget="code"))


class SchemaOutput(BaseModel):
    schema_: dict[str, object] = Field(serialization_alias="schema")


class ManifestsService(Service[ManifestsConfig, ManifestsState]):
    id = "manifests"
    title = "Manifests"
    description = "Validates each project's forge.project.toml against the contract and reports errors by line."
    Config = ManifestsConfig
    State = ManifestsState

    async def load(self, directory: Path) -> ManifestLoad:
        return await asyncio.to_thread(load_manifest, directory)

    def record(self, project_id: str, result: ManifestLoad) -> None:
        """Publish one project's manifest status."""
        status = ManifestStatus(project_id=project_id, path=result.path, valid=result.valid, issues=result.issues)
        others = [item for item in self.state.manifests if item.project_id != project_id]
        self.update_state(manifests=sorted([*others, status], key=lambda item: item.project_id))

    def forget(self, project_id: str) -> None:
        self.update_state(manifests=[item for item in self.state.manifests if item.project_id != project_id])

    @action(title="Validate manifest file", query=True)
    async def validate(self, request: ValidatePathInput) -> ManifestLoad:
        """Validate a manifest on disk."""
        return await self.load(Path(request.path))

    @action(title="Validate manifest text", query=True)
    async def validate_text(self, request: ValidateTextInput) -> ManifestLoad:
        """Validate manifest source text, e.g. while editing."""
        return parse_manifest_text(request.text)

    @action(title="Manifest JSON Schema", query=True)
    async def json_schema(self, _request: NoInput) -> SchemaOutput:
        """The forge.project.toml schema generated from the contract models."""
        return SchemaOutput(schema_=manifest_json_schema())
