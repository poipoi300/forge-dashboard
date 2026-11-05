"""`forge.project.toml` schema 1, as pydantic models.

These models are the source of truth for the manifest contract described in
`docs/project-manifest.md`; `schema/forge.project.schema.json` is generated
from them. Field-level rules live on the fields. Rules that relate fields
(placeholders allowed by sibling settings, unique ids, cross-references,
dependency cycles) live in model validators that raise `ManifestRuleViolation`
with locations relative to the model, so the loader can point at exact lines.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

from forge_dashboard.manifest import placeholders as ph

MANIFEST_FILENAME = "forge.project.toml"
SCHEMA_VERSION = 1

Location = tuple[str | int, ...]


class ManifestRuleViolation(ValueError):
    """One or more cross-field rule failures, each located relative to the failing model."""

    def __init__(self, problems: Sequence[tuple[Location, str]]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(message for _loc, message in self.problems))


def _raise_if_any(problems: list[tuple[Location, str]]) -> None:
    if problems:
        raise ManifestRuleViolation(problems)


_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _check_repo_path(value: str) -> str:
    if not value:
        raise ValueError("path must not be empty (use '.' for the repo root)")
    if "\\" in value:
        raise ValueError(f"{value!r}: use forward slashes in repo paths")
    if value.startswith("/") or _DRIVE_PREFIX.match(value):
        raise ValueError(f"{value!r}: paths are repo-relative; absolute paths are rejected")
    if ".." in value.split("/"):
        raise ValueError(f"{value!r}: '..' segments are rejected")
    return value


def _check_argv(value: list[str]) -> list[str]:
    if not value[0]:
        raise ValueError("argv[0] must name an executable")
    return value


def _check_language(value: str) -> str:
    if not value or value != value.lower() or any(char.isspace() for char in value):
        raise ValueError(f"{value!r}: language names are lowercase with no whitespace, e.g. 'python'")
    return value


def _check_regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as error:
        raise ValueError(f"{value!r} is not a valid regular expression: {error}") from error
    return value


def _check_url_path(value: str) -> str:
    if "://" in value:
        raise ValueError(f"{value!r}: give a URL path such as '/api/health', not a full URL")
    return value


def _check_docs(value: str) -> str:
    if "://" in value:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"{value!r}: docs URLs must be http(s)")
        return value
    return _check_repo_path(value)


def _check_params_schema(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if value.get("type") != "object":
        raise ValueError('params must be a JSON Schema with type = "object"')
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as error:
        raise ValueError(f"params is not a valid JSON Schema: {error.message}") from error
    return value


Slug = Annotated[str, StringConstraints(pattern=_SLUG_PATTERN)]
RepoPath = Annotated[
    str,
    AfterValidator(_check_repo_path),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "pattern": r"^(?![A-Za-z]:)(?!/)(?!(.*/)?\.\.(/|$))[^\\]*$",
            "description": "Repo-relative path with forward slashes; no absolute paths or '..' segments.",
        }
    ),
]
Argv = Annotated[
    list[str],
    Field(min_length=1, description="Command as an argv array; argv[0] is resolved on PATH."),
    AfterValidator(_check_argv),
]
Env = dict[str, str]
Language = Annotated[str, AfterValidator(_check_language)]
PortNumber = Annotated[int, Field(ge=1, le=65535)]
PositiveInt = Annotated[int, Field(gt=0)]
UrlPath = Annotated[str, AfterValidator(_check_url_path)]


class ManifestModel(BaseModel):
    """Base for every manifest table: unknown keys fail loudly."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _placeholder_problems(
    templates_by_field: Iterable[tuple[Location, Iterable[str]]], allowed: Iterable[str]
) -> tuple[set[str], list[tuple[Location, str]]]:
    """Return the placeholder names used across all fields, and located problems."""
    allowed_names = tuple(allowed)
    used: set[str] = set()
    problems: list[tuple[Location, str]] = []
    for loc, templates in templates_by_field:
        field_used, messages = ph.check_placeholders(templates, allowed_names)
        used |= field_used
        problems.extend((loc, message) for message in messages)
    return used, problems


def _duplicate_id_problems(table: str, ids: Sequence[str]) -> list[tuple[Location, str]]:
    seen: set[str] = set()
    problems: list[tuple[Location, str]] = []
    for index, item_id in enumerate(ids):
        if item_id in seen:
            problems.append(((table, index, "id"), f"duplicate id {item_id!r} in [[{table}]]"))
        seen.add(item_id)
    return problems


class Project(ManifestModel):
    """`[project]`: identity."""

    name: Annotated[str, Field(min_length=1, description="Display name.")]
    id: Annotated[Slug, Field(description="Stable identifier, unique across all projects the dashboard knows.")]
    summary: Annotated[str, Field(min_length=1, max_length=140, description="One-line description.")]
    kind: Literal["app", "service", "library", "cli", "tool", "game", "research"]
    languages: Annotated[list[Language], Field(description="Lowercase language names, e.g. python, rust.")]
    tags: list[str] = Field(default_factory=list, description="Free-form tags for filtering.")
    default_branch: str = Field("main", min_length=1, description="Branch that CI status and standards refer to.")
    homepage: HttpUrl | None = Field(None, description="Public page.")
    docs: Annotated[str, AfterValidator(_check_docs)] | None = Field(
        None, description="Documentation entry point: an http(s) URL or a repo path."
    )


class Milestone(ManifestModel):
    """One `[[v1.milestones]]` entry."""

    id: Slug
    title: Annotated[str, Field(min_length=1)]
    description: str | None = None
    weight: Annotated[int, Field(ge=1, description="Relative size.")] = 1
    done: bool = False


class V1(ManifestModel):
    """`[v1]`: V1 progress, from exactly one source."""

    source: Literal["milestones", "github-milestone"]
    target_date: date | None = Field(None, description="Planned V1 date.")
    github_milestone: str | None = Field(
        None, description='Required when source = "github-milestone": the GitHub milestone title.'
    )
    milestones: list[Milestone] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        problems: list[tuple[Location, str]] = []
        if self.source == "milestones":
            if not self.milestones:
                problems.append((("source",), 'source = "milestones" needs at least one [[v1.milestones]]'))
            if self.github_milestone is not None:
                problems.append(
                    (("github_milestone",), 'github_milestone is only valid with source = "github-milestone"')
                )
        else:
            if not self.github_milestone:
                problems.append((("source",), 'source = "github-milestone" needs github_milestone'))
            if self.milestones:
                problems.append((("milestones",), '[[v1.milestones]] is only valid with source = "milestones"'))
        problems.extend(_duplicate_id_problems("milestones", [milestone.id for milestone in self.milestones]))
        _raise_if_any(problems)
        return self


class Check(ManifestModel):
    """One `[[checks]]` entry: a local quality check."""

    id: Slug
    label: Annotated[str, Field(min_length=1)]
    argv: Argv
    cwd: RepoPath = "."
    env: Env = Field(default_factory=dict)
    timeout_s: PositiveInt = 900
    stage: Literal["commit", "push", "ci", "manual"] = Field(
        "manual", description="Where this check lives in the project's gate chain (informational)."
    )
    report: Literal["exit-code", "junit"] = Field(
        "exit-code", description="With junit, argv must contain {report_path}; the JUnit XML written there is parsed."
    )

    @model_validator(mode="after")
    def _placeholders(self) -> Self:
        allowed = [ph.PROJECT_ROOT] + ([ph.REPORT_PATH] if self.report == "junit" else [])
        argv_used, problems = _placeholder_problems([(("argv",), self.argv)], allowed)
        _env_used, env_problems = _placeholder_problems(
            [(("env", key), [value]) for key, value in self.env.items()], allowed
        )
        problems.extend(env_problems)
        if self.report == "junit" and ph.REPORT_PATH not in argv_used:
            problems.append((("argv",), 'report = "junit" needs {report_path} in argv'))
        _raise_if_any(problems)
        return self


class DynamicPort(ManifestModel):
    """`{ policy = "dynamic" }`: the dashboard picks a free port, trying `preferred` first."""

    policy: Literal["dynamic"]
    preferred: PortNumber | None = None


class PinnedPort(ManifestModel):
    """`{ policy = "pinned" }`: a fixed port that something outside the project hardcodes."""

    policy: Literal["pinned"]
    port: PortNumber
    evidence: Annotated[
        str,
        Field(
            min_length=1,
            description="Where something outside the project hardcodes the port. Convention is not evidence.",
        ),
    ]


PortPolicy = Annotated[DynamicPort | PinnedPort, Field(discriminator="policy")]


class PortReady(ManifestModel):
    """Ready when every declared port listens under a process in the service's tree."""

    kind: Literal["port"]


class HttpReady(ManifestModel):
    """Ready when GET `path` answers `expect_status`."""

    kind: Literal["http"]
    path: UrlPath
    expect_status: Annotated[int, Field(ge=100, le=599)] = 200


class LogReady(ManifestModel):
    """Ready when a stdout/stderr line matches `pattern`."""

    kind: Literal["log"]
    pattern: Annotated[str, AfterValidator(_check_regex)]


class NoReady(ManifestModel):
    """Ready as soon as the process starts."""

    kind: Literal["none"]


Readiness = Annotated[PortReady | HttpReady | LogReady | NoReady, Field(discriminator="kind")]


class Service(ManifestModel):
    """One `[[services]]` entry: a long-running process the dashboard launches."""

    id: Slug
    label: Annotated[str, Field(min_length=1)]
    description: str | None = None
    argv: Argv
    cwd: RepoPath = "."
    env: Env = Field(default_factory=dict)
    host: str = Field("127.0.0.1", min_length=1, description="Bind address passed as {host}.")
    port: PortPolicy | None = Field(None, description="Port policy; omit for portless services.")
    ready: Readiness | None = Field(
        None, description='Readiness signal. Defaults to { kind = "port" } when port is set, else { kind = "none" }.'
    )
    open: UrlPath | None = Field(None, description="URL path appended to http://{host}:{port} by the Open action.")
    depends_on: list[Slug] = Field(default_factory=list, description="Services started first.")

    @property
    def effective_ready(self) -> PortReady | HttpReady | LogReady | NoReady:
        """The readiness signal after applying the contract's default."""
        if self.ready is not None:
            return self.ready
        return PortReady(kind="port") if self.port is not None else NoReady(kind="none")

    @model_validator(mode="after")
    def _port_rules(self) -> Self:
        allowed = [ph.PROJECT_ROOT, ph.HOST] + ([ph.PORT] if self.port is not None else [])
        _used, problems = _placeholder_problems(
            [(("argv",), self.argv), *((("env", key), [value]) for key, value in self.env.items())], allowed
        )
        if self.port is None:
            if self.ready is not None and self.ready.kind in ("port", "http"):
                problems.append((("ready",), f"ready kind {self.ready.kind!r} needs a port policy"))
            if self.open is not None:
                problems.append((("open",), "open needs a port policy"))
        _raise_if_any(problems)
        return self


class DeploymentHealth(ManifestModel):
    """HTTP(S) health probe for a deployment."""

    path: UrlPath
    expect_status: Annotated[int, Field(ge=100, le=599)] = 200


class Deployment(ManifestModel):
    """One `[[deployments]]` entry: an endpoint that runs elsewhere and is monitored."""

    id: Slug
    label: Annotated[str, Field(min_length=1)]
    url: HttpUrl
    machine: str | None = Field(None, description="Host name or label of the machine it runs on.")
    environment: Literal["production", "staging", "preview", "dev"] = "production"
    health: DeploymentHealth | None = None


class MetricThresholds(ManifestModel):
    """Status thresholds, applied when a reading carries no status."""

    warn_below: float | None = None
    warn_above: float | None = None
    fail_below: float | None = None
    fail_above: float | None = None


class CommandSource(ManifestModel):
    """stdout must be exactly one JSON `MetricReading`; non-zero exit fails the reading."""

    kind: Literal["command"]
    argv: Argv
    cwd: RepoPath = "."
    timeout_s: PositiveInt = 60

    @model_validator(mode="after")
    def _placeholders(self) -> Self:
        _used, problems = _placeholder_problems([(("argv",), self.argv)], [ph.PROJECT_ROOT])
        _raise_if_any(problems)
        return self


class HttpSource(ManifestModel):
    """GET against a running service; the body is a `MetricReading`."""

    kind: Literal["http"]
    service: Slug
    path: UrlPath


class FileSource(ManifestModel):
    """A repo file holding a `MetricReading`."""

    kind: Literal["file"]
    path: RepoPath


MetricSource = Annotated[CommandSource | HttpSource | FileSource, Field(discriminator="kind")]


class Metric(ManifestModel):
    """One `[[metrics]]` entry: a project-defined value with history."""

    id: Slug
    label: Annotated[str, Field(min_length=1)]
    description: str | None = None
    unit: str | None = Field(None, description='Display unit, e.g. "files".')
    display: Literal["number", "percent", "duration", "bytes", "text", "status"] = Field(
        "number", description="How the value renders. percent expects 0-100; duration expects seconds."
    )
    interval_s: Annotated[int, Field(ge=30)] = 900
    pinned: bool = Field(False, description="Shown on the overview card.")
    thresholds: MetricThresholds | None = None
    source: MetricSource


class MetricReading(ManifestModel):
    """The JSON document a metric source produces."""

    value: bool | int | float | str
    status: Literal["ok", "warn", "fail"] | None = None
    detail: str | None = None


class Action(ManifestModel):
    """One `[[actions]]` entry: a project-defined operation with a self-built form."""

    id: Slug
    label: Annotated[str, Field(min_length=1)]
    description: str | None = None
    argv: Argv
    cwd: RepoPath = "."
    env: Env = Field(default_factory=dict)
    timeout_s: PositiveInt = 3600
    params: Annotated[dict[str, JsonValue], AfterValidator(_check_params_schema)] | None = Field(
        None,
        description='JSON Schema (type = "object") for the input; params arrive on stdin as one JSON document.',
    )
    confirm: bool = Field(False, description="Ask before running.")
    danger: bool = Field(False, description="Destructive styling and typed confirmation.")
    requires_service: Slug | None = Field(None, description="Service that must be running.")

    @model_validator(mode="after")
    def _placeholders(self) -> Self:
        _used, problems = _placeholder_problems(
            [(("argv",), self.argv), *((("env", key), [value]) for key, value in self.env.items())],
            [ph.PROJECT_ROOT],
        )
        _raise_if_any(problems)
        return self


class ActionLink(ManifestModel):
    """A link an action reports back."""

    label: str
    url: str


class ActionResult(ManifestModel):
    """Optional JSON document an action writes to `$FORGE_ACTION_RESULT`."""

    summary: str | None = None
    links: list[ActionLink] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class Launch(ManifestModel):
    """`[launch]`: launch and interface instructions."""

    instructions: str | None = Field(None, description="Markdown, or a repo-relative path to a .md file.")
    primary_service: Slug | None = Field(None, description="The service the Launch button starts and opens.")


class ForgeAlignment(ManifestModel):
    """`[forge]`: written by tools when standards were last aligned."""

    template_revision: str | None = Field(None, description="forge commit the repo's standards were last aligned to.")
    aligned_at: datetime | None = Field(None, description="When that alignment happened.")


class ProjectManifest(ManifestModel):
    """The whole `forge.project.toml` document, schema 1."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        title="forge.project.toml",
        json_schema_extra={"description": "forge project manifest, schema 1. See docs/project-manifest.md."},
    )

    schema_version: Literal[1] = Field(alias="schema", description="Manifest schema version. Must be 1.")
    project: Project
    v1: V1
    checks: list[Check] = Field(default_factory=list)
    services: list[Service] = Field(default_factory=list)
    deployments: list[Deployment] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    launch: Launch | None = None
    forge: ForgeAlignment | None = None

    def service(self, service_id: str) -> Service:
        """Return the service with `service_id`.

        Raises:
            KeyError: when no service has that id.
        """
        for service in self.services:
            if service.id == service_id:
                return service
        raise KeyError(service_id)

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        problems: list[tuple[Location, str]] = []
        for table, ids in (
            ("checks", [check.id for check in self.checks]),
            ("services", [service.id for service in self.services]),
            ("deployments", [deployment.id for deployment in self.deployments]),
            ("metrics", [metric.id for metric in self.metrics]),
            ("actions", [action.id for action in self.actions]),
        ):
            problems.extend(_duplicate_id_problems(table, ids))

        service_ids = {service.id for service in self.services}

        def unknown_service(loc: Location, ref: str) -> None:
            known = ", ".join(sorted(service_ids)) or "none declared"
            problems.append((loc, f"unknown service {ref!r} (services: {known})"))

        graph: dict[str, list[str]] = {}
        for index, service in enumerate(self.services):
            graph[service.id] = list(service.depends_on)
            for dep_index, dependency in enumerate(service.depends_on):
                if dependency == service.id:
                    problems.append((("services", index, "depends_on", dep_index), "a service cannot depend on itself"))
                elif dependency not in service_ids:
                    unknown_service(("services", index, "depends_on", dep_index), dependency)
        known_edges = {key: [dep for dep in deps if dep in service_ids and dep != key] for key, deps in graph.items()}
        try:
            TopologicalSorter(known_edges).prepare()
        except CycleError as error:
            cycle = " -> ".join(str(node) for node in error.args[1])
            problems.append((("services",), f"depends_on forms a cycle: {cycle}"))

        services_by_id = {service.id: service for service in self.services}
        for index, metric in enumerate(self.metrics):
            if not isinstance(metric.source, HttpSource):
                continue
            source_loc: Location = ("metrics", index, "source", "service")
            source_service = services_by_id.get(metric.source.service)
            if source_service is None:
                unknown_service(source_loc, metric.source.service)
            elif source_service.port is None:
                problems.append((source_loc, f"service {source_service.id!r} has no port to GET"))
        for index, action in enumerate(self.actions):
            if action.requires_service is not None and action.requires_service not in service_ids:
                unknown_service(("actions", index, "requires_service"), action.requires_service)
        primary_service = self.launch.primary_service if self.launch is not None else None
        if primary_service is not None and primary_service not in service_ids:
            unknown_service(("launch", "primary_service"), primary_service)
        _raise_if_any(problems)
        return self
