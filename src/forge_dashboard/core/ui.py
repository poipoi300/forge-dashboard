"""`x-ui` display hints carried in JSON Schema.

Models describe how they render; the frontend's schema renderer reads these
hints and builds forms, views, cards, and table columns from them, so adding
a field to a model makes it appear in the UI with no frontend edit.

The spec's vocabulary is extended with a few keys the renderer needs (marked
"extension" below); `docs/ARCHITECTURE.md` lists them.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict, Unpack

Widget = Literal[
    "text",
    "textarea",
    "number",
    "slider",
    "toggle",
    "select",
    "radio-cards",
    "multiselect",
    "tags",
    "path-dir",
    "path-file",
    "color",
    "duration",
    "cron",
    "secret",
    "code",
    "json",
    "model-picker",
]

DisplayFormat = Literal[
    "relative-time",
    "duration",
    "bytes",
    "percent",
    "progress",
    "badge",
    "code",
    "url",
    "path",
]

Tone = Literal["good", "warn", "bad", "info", "neutral"]


class VisibleIf(TypedDict):
    field: str
    equals: Any


class UiHints(TypedDict, total=False):
    widget: Widget
    group: str
    order: int
    advanced: bool
    hidden: bool
    unit: str
    placeholder: str
    help: str
    format: DisplayFormat
    visible_if: VisibleIf
    enum_labels: dict[str, str]
    enum_descriptions: dict[str, str]
    columns: list[str]
    # extension: fill the field from page context instead of asking ("project" -> current project id).
    bind: Literal["project"]
    # extension: tone per enum value for `badge` / status rendering (always paired with a label and glyph).
    tones: dict[str, Tone]
    # extension: slider bounds and step.
    min: float
    max: float
    step: float
    # extension: short label for dense surfaces (table headers, card captions).
    short_label: str
    # extension: card slot on the overview card ("title", "meta", "status", "signals", "body", "footer").
    slot: str


def ui(**hints: Unpack[UiHints]) -> dict[str, Any]:
    """Return a `json_schema_extra` dict carrying one `x-ui` object."""
    return {"x-ui": dict(hints)}


def with_hints(base: dict[str, Any], **hints: Unpack[UiHints]) -> dict[str, Any]:
    """Extend a shared `ui(...)` dict with field-specific hints."""
    return {"x-ui": {**base["x-ui"], **hints}}
