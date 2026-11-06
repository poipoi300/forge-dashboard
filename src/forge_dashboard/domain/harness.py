"""Agent harness configuration, argv rendering, and stream-json transcript parsing.

A harness has the same shape as forge.toml's `[harness.*]`: an argv template
with `{prompt}` and `{prompt_path}`. Per-run options (model, permission mode,
allowed tools, turn limit) are argument templates of their own, so a harness
that lacks one simply leaves it empty. The default is `claude -p` with
`--output-format stream-json --verbose`, the prompt on stdin (briefs exceed
Windows' command-line limit), which yields a live transcript plus the
result's cost, turns, and duration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, Field, JsonValue

from forge_dashboard.core.ui import ui

PermissionMode = Literal["default", "acceptEdits", "bypassPermissions", "plan"]


class HarnessConfig(BaseModel):
    """How to invoke the agent CLI."""

    command: list[str] = Field(
        default_factory=lambda: ["claude", "-p", "--output-format", "stream-json", "--verbose"],
        json_schema_extra=ui(
            widget="tags",
            help="Argv template; may use {prompt} and {prompt_path}. Output lines that are not stream-json "
            "records go to the job log, so a plain-text agent runs without a transcript or cost.",
        ),
    )
    stdin: str | None = Field(
        "{prompt}", json_schema_extra=ui(help="Template written to stdin; empty to pass nothing on stdin.")
    )
    model_args: list[str] = Field(default_factory=lambda: ["--model", "{model}"], json_schema_extra=ui(widget="tags"))
    permission_args: list[str] = Field(
        default_factory=lambda: ["--permission-mode", "{permission_mode}"], json_schema_extra=ui(widget="tags")
    )
    allowed_tools_args: list[str] = Field(
        default_factory=lambda: ["--allowedTools", "{tools}"], json_schema_extra=ui(widget="tags")
    )
    disallowed_tools_args: list[str] = Field(
        default_factory=lambda: ["--disallowedTools", "{tools}"], json_schema_extra=ui(widget="tags")
    )
    max_turns_args: list[str] = Field(
        default_factory=lambda: ["--max-turns", "{max_turns}"], json_schema_extra=ui(widget="tags")
    )


class RunKindSettings(BaseModel):
    """Options for one kind of agent run."""

    model: str | None = Field(None, json_schema_extra=ui(widget="model-picker", help="Empty uses the CLI default."))
    permission_mode: PermissionMode = Field("acceptEdits", json_schema_extra=ui(widget="select"))
    allowed_tools: list[str] = Field(default_factory=list, json_schema_extra=ui(widget="tags"))
    disallowed_tools: list[str] = Field(default_factory=list, json_schema_extra=ui(widget="tags"))
    max_turns: int | None = Field(None, ge=1, le=500)
    timeout_s: int = Field(1800, ge=60, le=24 * 3600, json_schema_extra=ui(widget="duration"))


EDIT_ONLY_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"]


def _edit_only() -> RunKindSettings:
    """The agent edits files; the dashboard runs every git command itself."""
    return RunKindSettings(allowed_tools=list(EDIT_ONLY_TOOLS), disallowed_tools=["Bash", "WebFetch", "WebSearch"])


# The commands implementation work needs: tests, linters, type checks, and dependency tools. Delegated
# issues carry text anyone can write, so a run may not reach for arbitrary commands, the network, or git.
PROJECT_COMMANDS = [
    "Bash(uv run:*)",
    "Bash(uv sync:*)",
    "Bash(uv lock:*)",
    "Bash(uv add:*)",
    "Bash(uv remove:*)",
    "Bash(bun test:*)",
    "Bash(bun run:*)",
    "Bash(bun install:*)",
    "Bash(bun add:*)",
    "Bash(bun remove:*)",
    "Bash(cargo test:*)",
    "Bash(cargo build:*)",
    "Bash(cargo check:*)",
    "Bash(cargo clippy:*)",
    "Bash(cargo fmt:*)",
    "Bash(cargo update:*)",
]


def _with_project_commands() -> RunKindSettings:
    """The agent edits files and runs the project's own tools; git stays with the dashboard."""
    return RunKindSettings(
        allowed_tools=[*EDIT_ONLY_TOOLS, *PROJECT_COMMANDS], disallowed_tools=["WebFetch", "WebSearch"]
    )


class RunKinds(BaseModel):
    """Per-kind options. Conflict resolution is edit-only; implementation work may run the project's own tools."""

    fork_sync: RunKindSettings = Field(default_factory=_edit_only, json_schema_extra=ui(group="Fork sync"))
    standards: RunKindSettings = Field(default_factory=_edit_only, json_schema_extra=ui(group="Bring up to standards"))
    adopt: RunKindSettings = Field(default_factory=_edit_only, json_schema_extra=ui(group="Adopt"))
    delegate: RunKindSettings = Field(
        default_factory=_with_project_commands, json_schema_extra=ui(group="Delegate issue")
    )
    update_deps: RunKindSettings = Field(
        default_factory=_with_project_commands, json_schema_extra=ui(group="Update dependencies")
    )


RunKind = Literal["fork_sync", "standards", "adopt", "delegate", "update_deps"]


def render_argv(harness: HarnessConfig, settings: RunKindSettings, prompt: str, prompt_path: str) -> list[str]:
    """The concrete argv for one run (placeholders substituted, empty options omitted)."""
    values = {"prompt": prompt, "prompt_path": prompt_path}

    def substitute(parts: Iterable[str], extra: Mapping[str, str]) -> list[str]:
        merged = {**values, **extra}
        return [_fill(part, merged) for part in parts]

    argv = substitute(harness.command, {})
    if settings.model:
        argv += substitute(harness.model_args, {"model": settings.model})
    argv += substitute(harness.permission_args, {"permission_mode": settings.permission_mode})
    if settings.allowed_tools:
        argv += substitute(harness.allowed_tools_args, {"tools": ",".join(settings.allowed_tools)})
    if settings.disallowed_tools:
        argv += substitute(harness.disallowed_tools_args, {"tools": ",".join(settings.disallowed_tools)})
    if settings.max_turns is not None:
        argv += substitute(harness.max_turns_args, {"max_turns": str(settings.max_turns)})
    return argv


def render_stdin(harness: HarnessConfig, prompt: str, prompt_path: str) -> bytes | None:
    if harness.stdin is None or harness.stdin == "":
        return None
    return _fill(harness.stdin, {"prompt": prompt, "prompt_path": prompt_path}).encode("utf-8")


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _fill(template: str, values: Mapping[str, str]) -> str:
    """Replace `{name}` for known names only, in one pass so a value is never scanned for placeholders itself.

    Unknown names and other braces stay as written: the prompt text lands in the same templates as the names.
    """
    return _PLACEHOLDER.sub(lambda match: values.get(match[1], match[0]), template)


class RunResult(BaseModel):
    """The `result` record of a stream-json transcript."""

    is_error: bool = False
    subtype: str | None = None
    result: str | None = None
    cost_usd: float | None = Field(None, json_schema_extra=ui(unit="USD"))
    turns: int | None = None
    duration_ms: int | None = Field(None, json_schema_extra=ui(format="duration"))
    session_id: str | None = None


def parse_record(line: str) -> dict[str, JsonValue] | None:
    """One stream-json line as a dict; None for blank or non-JSON lines (the CLI may print warnings)."""
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        inner = value.get("text") if "text" in value else value.get("content")
        return _text(inner) if inner is not None else ""
    return ""


def _one_line(text: str, limit: int = 240) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _tool_target(arguments: JsonValue) -> str:
    """What a tool call acts on: a file, a search pattern, or a command."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("file_path", "pattern", "command"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def describe_record(record: Mapping[str, JsonValue]) -> list[str]:
    """Human log lines for one transcript record."""
    kind = record.get("type")
    if kind == "system" and record.get("subtype") == "init":
        return [f"agent: session started (model {record.get('model', '?')})"]
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = content if isinstance(content, list) else []
    if kind == "assistant":
        lines: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                lines.append(f"agent: {_one_line(_text(block.get('text')))}")
            elif block.get("type") == "tool_use":
                lines.append(f"tool: {block.get('name')} {_one_line(_tool_target(block.get('input')), 160)}".rstrip())
        return lines
    if kind == "user":
        errors = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")]
        return [f"tool error: {_one_line(_text(block.get('content')))}" for block in errors]
    if kind == "result":
        result = result_of(record)
        cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "cost n/a"
        return [f"result: {result.subtype or 'done'}, {cost}, {result.turns} turns, {result.duration_ms} ms"]
    return []


def result_of(record: Mapping[str, JsonValue]) -> RunResult:
    def number(key: str) -> float | None:
        value = record.get(key)
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

    turns = number("num_turns")
    duration = number("duration_ms")
    subtype = record.get("subtype")
    result = record.get("result")
    session = record.get("session_id")
    return RunResult(
        is_error=bool(record.get("is_error")),
        subtype=subtype if isinstance(subtype, str) else None,
        result=result if isinstance(result, str) else None,
        cost_usd=number("total_cost_usd") if number("total_cost_usd") is not None else number("cost_usd"),
        turns=int(turns) if turns is not None else None,
        duration_ms=int(duration) if duration is not None else None,
        session_id=session if isinstance(session, str) else None,
    )
