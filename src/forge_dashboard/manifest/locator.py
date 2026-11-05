"""Map TOML key paths to the line they are written on.

`tomllib` returns values without positions, but validation errors must point at
lines. This scanner walks a document that `tomllib` has already accepted and
records the line of every table header, key, and array element, keyed by the
same concrete path pydantic reports (`("checks", 1, "argv", 2)`). Because the
input is known to be valid TOML, the scanner only has to recognise structure,
not reject malformed syntax.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

Path = tuple[str | int, ...]

_BARE_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_SCALAR_TERMINATORS = frozenset(",]}#\n")


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.line = 1
        self.lines: dict[Path, int] = {}
        self._array_table_index: dict[Path, int] = {}

    # --- low-level cursor ---

    def _peek(self, offset: int = 0) -> str:
        index = self.pos + offset
        return self.text[index] if index < len(self.text) else ""

    def _starts(self, token: str) -> bool:
        return self.text.startswith(token, self.pos)

    def _advance(self, count: int = 1) -> None:
        for _ in range(count):
            if self.pos >= len(self.text):
                return
            if self.text[self.pos] == "\n":
                self.line += 1
            self.pos += 1

    def _skip_inline_space(self) -> None:
        while self._peek() in (" ", "\t", "\r") and self._peek():
            self._advance()

    def _skip_comment(self) -> None:
        while self._peek() and self._peek() != "\n":
            self._advance()

    def _skip_space_newlines_comments(self) -> None:
        while True:
            char = self._peek()
            if char in (" ", "\t", "\r", "\n") and char:
                self._advance()
            elif char == "#":
                self._skip_comment()
            else:
                return

    def _record(self, path: Path, line: int) -> None:
        self.lines.setdefault(path, line)

    # --- strings and keys ---

    def _read_basic_string(self) -> str:
        self._advance()  # opening quote
        chars: list[str] = []
        while self._peek() and self._peek() != '"':
            if self._peek() == "\\":
                chars.append(self._peek(1))
                self._advance(2)
                continue
            chars.append(self._peek())
            self._advance()
        self._advance()  # closing quote
        return "".join(chars)

    def _read_literal_string(self) -> str:
        self._advance()
        start = self.pos
        while self._peek() and self._peek() != "'":
            self._advance()
        value = self.text[start : self.pos]
        self._advance()
        return value

    def _skip_multiline_string(self, delimiter: str) -> None:
        self._advance(3)
        while self.pos < len(self.text):
            if delimiter == '"""' and self._peek() == "\\":
                self._advance(2)
                continue
            if self._starts(delimiter):
                # A closing delimiter may be followed by up to two extra quotes that belong to the content.
                self._advance(3)
                while self._peek() == delimiter[0]:
                    self._advance()
                return
            self._advance()

    def _read_key(self) -> list[str]:
        parts: list[str] = []
        while True:
            self._skip_inline_space()
            char = self._peek()
            if char == '"':
                parts.append(self._read_basic_string())
            elif char == "'":
                parts.append(self._read_literal_string())
            else:
                start = self.pos
                while self._peek() in _BARE_KEY_CHARS and self._peek():
                    self._advance()
                parts.append(self.text[start : self.pos])
            self._skip_inline_space()
            if self._peek() != ".":
                return parts
            self._advance()

    # --- values ---

    def _value(self, path: Path) -> None:
        if self._starts('"""') or self._starts("'''"):
            self._skip_multiline_string(self.text[self.pos : self.pos + 3])
        elif self._peek() == '"':
            self._read_basic_string()
        elif self._peek() == "'":
            self._read_literal_string()
        elif self._peek() == "[":
            self._array(path)
        elif self._peek() == "{":
            self._inline_table(path)
        else:
            while self._peek() and self._peek() not in _SCALAR_TERMINATORS:
                self._advance()

    def _array(self, path: Path) -> None:
        self._advance()  # [
        index = 0
        while True:
            self._skip_space_newlines_comments()
            if self._peek() in ("]", ""):
                self._advance()
                return
            self._record((*path, index), self.line)
            self._value((*path, index))
            self._skip_space_newlines_comments()
            if self._peek() == ",":
                self._advance()
                index += 1

    def _inline_table(self, path: Path) -> None:
        self._advance()  # {
        while True:
            self._skip_inline_space()
            if self._peek() in ("}", ""):
                self._advance()
                return
            line = self.line
            key_path = (*path, *self._read_key())
            self._record(key_path, line)
            self._skip_inline_space()
            self._advance()  # =
            self._skip_inline_space()
            self._value(key_path)
            self._skip_inline_space()
            if self._peek() == ",":
                self._advance()

    # --- statements ---

    def _resolve_header(self, parts: Sequence[str], array_table: bool) -> Path:
        concrete: Path = ()
        for depth, part in enumerate(parts):
            concrete = (*concrete, part)
            is_last = depth == len(parts) - 1
            if is_last and array_table:
                index = self._array_table_index.get(concrete, -1) + 1
                self._array_table_index[concrete] = index
                self._record(concrete, self.line)
                concrete = (*concrete, index)
            elif concrete in self._array_table_index:
                concrete = (*concrete, self._array_table_index[concrete])
        return concrete

    def scan(self) -> dict[Path, int]:
        table: Path = ()
        while self.pos < len(self.text):
            self._skip_space_newlines_comments()
            if not self._peek():
                break
            if self._peek() == "[":
                array_table = self._starts("[[")
                self._advance(2 if array_table else 1)
                line = self.line
                parts = self._read_key()
                self._advance(2 if array_table else 1)
                table = self._resolve_header(parts, array_table)
                self._record(table, line)
                continue
            line = self.line
            key_path = (*table, *self._read_key())
            self._record(key_path, line)
            self._skip_inline_space()
            self._advance()  # =
            self._skip_inline_space()
            self._value(key_path)
        return self.lines


def key_lines(text: str) -> dict[Path, int]:
    """Return the 1-based line of every key, header, and array element in a valid TOML document."""
    return _Scanner(text).scan()


def concrete_path(data: Mapping[str, Any], loc: Sequence[str | int]) -> Path:
    """Translate a pydantic error location into a document path.

    pydantic inserts the discriminator value into locations inside
    discriminated unions (`("services", 0, "port", "dynamic", "preferred")`).
    A tag always equals the discriminator field's value in the table being
    validated, which is how it is recognised and dropped. Segments past a key
    the document lacks (a missing required field) are kept, so the location
    still names what is missing while `line_for` falls back to the ancestor.
    """
    node: Any = data
    path: list[str | int] = []
    for segment in loc:
        if isinstance(node, Mapping):
            if segment in node:
                node = node[segment]
                path.append(segment)
                continue
            if isinstance(segment, str) and segment in {value for value in node.values() if isinstance(value, str)}:
                continue
            node = None
        elif isinstance(node, list) and isinstance(segment, int) and 0 <= segment < len(node):
            node = node[segment]
        else:
            node = None
        path.append(segment)
    return tuple(path)


def line_for(lines: Mapping[Path, int], path: Path) -> int | None:
    """Return the line of `path`, or of its nearest recorded ancestor."""
    for length in range(len(path), 0, -1):
        line = lines.get(path[:length])
        if line is not None:
            return line
    return None
