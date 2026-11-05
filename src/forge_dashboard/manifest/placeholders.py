"""`{name}` placeholders in manifest argv and env values.

The contract allows a fixed set of names per table and writes literal braces
as `{{` / `}}`, which is exactly `str.format` syntax, so parsing and rendering
both go through `string.Formatter` rather than a hand-rolled scanner.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from string import Formatter

_FORMATTER = Formatter()

PROJECT_ROOT = "project_root"
PORT = "port"
HOST = "host"
REPORT_PATH = "report_path"


def placeholder_names(template: str) -> set[str]:
    """Return the placeholder names used in `template`.

    Raises:
        ValueError: on unbalanced braces, an empty `{}`, or a conversion or
            format spec (`{port!r}`, `{port:5}`), none of which the contract
            allows.
    """
    names: set[str] = set()
    for _literal, field_name, format_spec, conversion in _FORMATTER.parse(template):
        if field_name is None:
            continue
        if not field_name:
            raise ValueError("empty placeholder '{}' (write literal braces as '{{' and '}}')")
        if format_spec or conversion:
            raise ValueError(f"placeholder '{{{field_name}}}' must not carry a conversion or format spec")
        names.add(field_name)
    return names


def check_placeholders(templates: Iterable[str], allowed: Iterable[str]) -> tuple[set[str], list[str]]:
    """Return the names the well-formed templates use, and one problem per bad template.

    A template is bad when it is malformed or uses a name outside `allowed`.
    """
    allowed_names = set(allowed)
    used_names: set[str] = set()
    problems: list[str] = []
    for template in templates:
        try:
            used = placeholder_names(template)
        except ValueError as error:
            problems.append(f"{template!r}: {error}")
            continue
        used_names |= used
        unknown = sorted(used - allowed_names)
        if unknown:
            listed = ", ".join(f"{{{name}}}" for name in unknown)
            permitted = ", ".join(f"{{{name}}}" for name in sorted(allowed_names)) or "none"
            problems.append(f"{template!r} uses {listed}; allowed here: {permitted}")
    return used_names, problems


def render(template: str, values: Mapping[str, str]) -> str:
    """Substitute placeholders, turning `{{` / `}}` back into literal braces."""
    return _FORMATTER.vformat(template, (), values)
