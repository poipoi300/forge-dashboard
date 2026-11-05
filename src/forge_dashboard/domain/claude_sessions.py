"""Claude Code activity in a project: transcript sessions and running processes.

Claude Code writes one transcript per session to
`<projects dir>/<encoded cwd>/<session id>.jsonl`, where the directory name is
the session's starting directory with every character that is not an ASCII
letter or digit replaced by `-`. The encoding is lossy (`ZombieMashups-lanes`
and `ZombieMashups/lanes` collide), so a directory whose name merely extends a
project's encoded path is only that project's when the transcript's recorded
`cwd` really lies inside the project.

Transcripts are appended to while a session runs and reach megabytes, so each
one is parsed once and then only from the end of what was already read.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psutil
from pydantic import BaseModel, Field

from forge_dashboard.core.ui import ui
from forge_dashboard.domain.summary import AgentFacts
from forge_dashboard.process.ports import path_is_within

MAX_PROMPT_CHARS = 200
MAX_ARGUMENT_CHARS = 200
# Enough of the file's start to tell an append from a rewrite.
HEAD_BYTES = 1024
CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code"

_NOT_ASCII_ALNUM = re.compile(r"[^A-Za-z0-9]")
# Slash-command echoes and interruption notices are user records but never a prompt.
_NOT_A_PROMPT = re.compile(r"^\s*(?:<(?:local-)?command-|\[Request interrupted)")
_SECRET_FLAG = re.compile(
    r"^--?(?:[\w-]*[-_])?(?:api[-_]?key|secret|password|passwd|credentials?|token)$", re.IGNORECASE
)
_LAUNCHERS = frozenset({"node", "bun"})


def encode_project_path(path: str | Path) -> str:
    """The directory name Claude Code uses under its projects dir for sessions started in `path`.

    Every character that is not an ASCII letter or digit becomes `-`:
    `C:\\Users\\Administrator\\.claude` is `C--Users-Administrator--claude`, and
    `Français` is `Fran-ais`.
    """
    return _NOT_ASCII_ALNUM.sub("-", str(path))


def default_projects_dir() -> Path:
    """Claude Code's transcript root: `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(configured) if configured else Path.home() / ".claude") / "projects"


class ClaudeSession(BaseModel):
    """One Claude Code session recorded in a transcript."""

    id: str = Field(json_schema_extra=ui(format="code"))
    start: float = Field(json_schema_extra=ui(format="relative-time"))
    end: float = Field(json_schema_extra=ui(format="relative-time", help="The last recorded activity."))
    duration_s: float = Field(json_schema_extra=ui(format="duration", short_label="duration"))
    first_prompt: str | None = Field(None, json_schema_extra=ui(help="First line of the first prompt typed."))
    cwd: str | None = Field(None, json_schema_extra=ui(format="path", help="Where the session started."))
    message_count: int = Field(json_schema_extra=ui(short_label="messages", help="User and assistant records."))
    transcript_path: str = Field(json_schema_extra=ui(format="path"))


class ClaudeProcess(BaseModel):
    """A running Claude Code process."""

    pid: int
    started_at: float = Field(json_schema_extra=ui(format="relative-time", short_label="started"))
    cwd: str = Field(json_schema_extra=ui(format="path"))
    cmdline: list[str] = Field(
        default_factory=list, json_schema_extra=ui(help="Secret-bearing arguments dropped, long ones truncated.")
    )


# --- transcript parsing ---


def _decode_record(line: bytes) -> dict[str, Any] | None:
    """One JSON object per line; a half-written or garbled line is skipped, not fatal."""
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _epoch(record: Mapping[str, Any]) -> float | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)).timestamp()


def _first_text(content: object) -> str | None:
    """The text of a user message whose content is a string or a list of blocks; tool results have none."""
    if isinstance(content, str):
        return content if content.strip() else None
    if not isinstance(content, list):
        return None
    blocks = [block for block in content if isinstance(block, dict)]
    if any(block.get("type") == "tool_result" for block in blocks):
        return None
    for block in blocks:
        text = block.get("text")
        if block.get("type") == "text" and isinstance(text, str) and text.strip():
            return text
    return None


def _one_line(text: str) -> str:
    first_line = next(line for line in text.splitlines() if line.strip())
    collapsed = " ".join(first_line.split())
    return collapsed if len(collapsed) <= MAX_PROMPT_CHARS else collapsed[: MAX_PROMPT_CHARS - 1] + "…"


def _prompt_line(record: Mapping[str, Any]) -> str | None:
    """The first line of a prompt the user typed (or an SDK caller sent), else None."""
    if record.get("isMeta") or record.get("isCompactSummary") or "toolUseResult" in record:
        return None
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return None
    message = record.get("message")
    text = _first_text(message.get("content") if isinstance(message, dict) else None)
    if text is None or _NOT_A_PROMPT.match(text):
        return None
    return _one_line(text)


@dataclass
class _Transcript:
    """What has been learned about one transcript, extended as the file grows."""

    head: bytes = b""
    offset: int = 0
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    cwd: str | None = None
    first_prompt: str | None = None
    message_count: int = 0

    def absorb(self, record: Mapping[str, Any]) -> None:
        timestamp = _epoch(record)
        if timestamp is not None:
            self.first_timestamp = timestamp if self.first_timestamp is None else min(self.first_timestamp, timestamp)
            self.last_timestamp = timestamp if self.last_timestamp is None else max(self.last_timestamp, timestamp)
        kind = record.get("type")
        if kind in ("user", "assistant"):
            self.message_count += 1
        cwd = record.get("cwd")
        if self.cwd is None and isinstance(cwd, str) and cwd:
            self.cwd = cwd
        if self.first_prompt is None and kind == "user":
            self.first_prompt = _prompt_line(record)

    def session(self, path: Path) -> ClaudeSession | None:
        """None until the transcript holds a timestamped message (a stub left by opening and closing Claude)."""
        if self.first_timestamp is None or self.last_timestamp is None or self.message_count == 0:
            return None
        return ClaudeSession(
            id=path.stem,
            start=self.first_timestamp,
            end=self.last_timestamp,
            duration_s=self.last_timestamp - self.first_timestamp,
            first_prompt=self.first_prompt,
            cwd=self.cwd,
            message_count=self.message_count,
            transcript_path=str(path),
        )


@dataclass
class _Entry:
    stamp: tuple[int, int]
    transcript: _Transcript
    session: ClaudeSession | None


class TranscriptIndex:
    """Parses each transcript once, then only the bytes appended since.

    A file is re-read from the start when it shrank or its first bytes changed
    (a rewrite); otherwise reading resumes after the last complete line, so a
    line still being written is picked up on the next call.
    """

    def __init__(self) -> None:
        self._entries: dict[Path, _Entry] = {}
        self._starting_cwds: dict[Path, str] = {}

    def session(self, path: Path) -> ClaudeSession | None:
        """The session in `path`, or None when it holds no conversation yet or was deleted meanwhile."""
        try:
            status = path.stat()
            stamp = (status.st_size, status.st_mtime_ns)
            entry = self._entries.get(path)
            if entry is not None and entry.stamp == stamp:
                return entry.session
            transcript = self._read_new_records(path, status.st_size, entry.transcript if entry else None)
        except FileNotFoundError:
            self._entries.pop(path, None)
            return None
        session = transcript.session(path)
        self._entries[path] = _Entry(stamp, transcript, session)
        return session

    def starting_cwd(self, path: Path) -> str | None:
        """The first `cwd` the transcript records, found without parsing the rest of a possibly huge file."""
        known = self._starting_cwds.get(path)
        if known is None:
            known = _peek_cwd(path)
            if known is not None:
                self._starting_cwds[path] = known
        return known

    @staticmethod
    def _read_new_records(path: Path, size: int, known: _Transcript | None) -> _Transcript:
        with path.open("rb") as handle:
            head = handle.read(HEAD_BYTES)
            appended = known is not None and size >= known.offset and head.startswith(known.head)
            transcript = known if known is not None and appended else _Transcript()
            transcript.head = head
            handle.seek(transcript.offset)
            chunk = handle.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]
        transcript.offset += len(complete)
        for line in complete.split(b"\n"):
            record = _decode_record(line) if line.strip() else None
            if record is not None:
                transcript.absorb(record)
        return transcript


def _peek_cwd(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    return None
                record = _decode_record(line) if line.strip() else None
                cwd = record.get("cwd") if record is not None else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except FileNotFoundError:
        return None
    return None


DEFAULT_INDEX = TranscriptIndex()


def _directory_relation(directory_name: str, encoded: str) -> Literal["exact", "extends"] | None:
    """`exact`, `extends` (a longer name that may be a subdirectory), or None, ignoring case where paths do."""
    name, wanted = os.path.normcase(directory_name), os.path.normcase(encoded)
    if name == wanted:
        return "exact"
    return "extends" if name.startswith(wanted + "-") else None


def sessions_for_project(
    projects_dir: Path, project_path: Path, *, index: TranscriptIndex = DEFAULT_INDEX
) -> list[ClaudeSession]:
    """Every Claude Code session started in the project or below it, latest activity first.

    The exact-match directory is the project's. A directory whose name only
    starts with the encoded path counts when its transcript's `cwd` lies
    inside `project_path`.
    """
    if not projects_dir.is_dir():
        return []
    encoded = encode_project_path(project_path)
    inside: dict[str, bool] = {}
    sessions: list[ClaudeSession] = []
    for directory in sorted(projects_dir.iterdir()):
        relation = _directory_relation(directory.name, encoded)
        if relation is None or not directory.is_dir():
            continue
        for transcript in sorted(directory.glob("*.jsonl")):
            if relation == "extends":
                cwd = index.starting_cwd(transcript)
                if cwd is None:
                    continue
                if cwd not in inside:
                    inside[cwd] = path_is_within(cwd, project_path)
                if not inside[cwd]:
                    continue
            session = index.session(transcript)
            if session is not None:
                sessions.append(session)
    return sorted(sessions, key=lambda session: (-session.end, session.id))


# --- running processes ---


def _executable_stem(name: str) -> str:
    return name.lower().removesuffix(".exe")


def is_claude_process(name: str, cmdline: Sequence[str]) -> bool:
    """A `claude` executable, or node/bun running the Claude Code package (not wrappers that merely mention it)."""
    stem = _executable_stem(name)
    if stem == "claude":
        return True
    return stem in _LAUNCHERS and any(CLAUDE_CODE_PACKAGE in argument.replace("\\", "/") for argument in cmdline)


def redact_cmdline(cmdline: Iterable[str]) -> list[str]:
    """Drop the value of every `--api-key`-like flag and truncate long arguments (prompts)."""
    redacted: list[str] = []
    drop_next = False
    for argument in cmdline:
        if drop_next:
            drop_next = False
            continue
        flag, equals, _value = argument.partition("=")
        if _SECRET_FLAG.match(flag):
            redacted.append(flag)
            drop_next = not equals
            continue
        redacted.append(argument if len(argument) <= MAX_ARGUMENT_CHARS else argument[: MAX_ARGUMENT_CHARS - 1] + "…")
    return redacted


def _inspect(process: psutil.Process) -> ClaudeProcess | None:
    """The process as a `ClaudeProcess`, or None when it is not Claude Code or cannot be read."""
    try:
        with process.oneshot():
            cmdline = process.cmdline()
            if not is_claude_process(process.name(), cmdline):
                return None
            return ClaudeProcess(
                pid=process.pid, started_at=process.create_time(), cwd=process.cwd(), cmdline=redact_cmdline(cmdline)
            )
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return None


def running_claude_processes() -> list[ClaudeProcess]:
    """Every running Claude Code process whose working directory is readable, by pid.

    Command lines are read only for processes that could be Claude Code,
    which keeps the scan to a fraction of a second on Windows.
    """
    candidates = {"claude", *_LAUNCHERS}
    found = (
        _inspect(process)
        for process in psutil.process_iter(["name"])
        if _executable_stem(process.info["name"] or "") in candidates
    )
    return sorted((process for process in found if process is not None), key=lambda process: process.pid)


def processes_inside(processes: Iterable[ClaudeProcess], project_path: Path) -> list[ClaudeProcess]:
    """The processes whose working directory is inside `project_path`."""
    return [process for process in processes if path_is_within(process.cwd, project_path)]


def active_claude_processes(project_path: Path) -> list[ClaudeProcess]:
    """Running Claude Code processes whose working directory is inside `project_path`.

    Scans the whole process table; to ask about many projects, call
    `running_claude_processes` once and filter with `processes_inside`.
    """
    return processes_inside(running_claude_processes(), project_path)


def agent_facts(sessions: Sequence[ClaudeSession], processes: Sequence[ClaudeProcess]) -> AgentFacts:
    """The overview's agent facts for one project."""
    return AgentFacts(
        active=bool(processes),
        last_session_at=max((session.end for session in sessions), default=None),
        session_count=len(sessions),
    )
