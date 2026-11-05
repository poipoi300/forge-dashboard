"""Claude Code transcripts and processes: path encoding, session extraction, caching, process matching."""

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import pytest

from forge_dashboard.domain import claude_sessions
from forge_dashboard.domain.claude_sessions import (
    MAX_ARGUMENT_CHARS,
    MAX_PROMPT_CHARS,
    ClaudeProcess,
    ClaudeSession,
    TranscriptIndex,
    active_claude_processes,
    agent_facts,
    default_projects_dir,
    encode_project_path,
    is_claude_process,
    processes_inside,
    redact_cmdline,
    running_claude_processes,
    sessions_for_project,
)

# Directory names are the ones found under a real ~/.claude/projects.
ENCODING_EXAMPLES = [
    (r"C:\Users\Administrator\.claude", "C--Users-Administrator--claude"),
    (
        r"C:\Users\Administrator\Documents\Projects\Tests\forge",
        "C--Users-Administrator-Documents-Projects-Tests-forge",
    ),
    (
        "C:\\Users\\Administrator\\Documents\\Projects\\Notes\\Fran\u00e7ais",
        "C--Users-Administrator-Documents-Projects-Notes-Fran-ais",
    ),
    (
        "C:\\Users\\Administrator\\Documents\\Projects\\Notes\\\u00c9duc",
        "C--Users-Administrator-Documents-Projects-Notes--duc",
    ),
    (r"E:\AI Images\Scripts", "E--AI-Images-Scripts"),
    ("/home/dev/my_app", "-home-dev-my-app"),
]


@pytest.mark.parametrize(("path", "encoded"), ENCODING_EXAMPLES)
def test_every_non_alphanumeric_character_becomes_a_dash(path: str, encoded: str) -> None:
    assert encode_project_path(path) == encoded


def test_encoding_accepts_path_objects() -> None:
    assert encode_project_path(Path("a") / "b.c") == "a-b-c"


def test_projects_dir_follows_the_claude_config_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    assert default_projects_dir() == tmp_path / "custom" / "projects"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert default_projects_dir() == Path.home() / ".claude" / "projects"


# --- transcript fixtures ---


def stamp(minutes: float) -> str:
    """An ISO timestamp the way Claude Code writes them, `minutes` after a fixed origin."""
    origin = datetime(2026, 9, 5, 7, 0, tzinfo=UTC).timestamp()
    return (
        datetime.fromtimestamp(origin + minutes * 60, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def epoch(minutes: float) -> float:
    return datetime(2026, 9, 5, 7, 0, tzinfo=UTC).timestamp() + minutes * 60


def user(minutes: float, content: object, cwd: Path | str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": stamp(minutes),
        "cwd": str(cwd),
        "message": {"role": "user", "content": content},
        **extra,
    }


def assistant(minutes: float, cwd: Path | str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": stamp(minutes),
        "cwd": str(cwd),
        "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    }


def write_transcript(directory: Path, session_id: str, records: Sequence[dict[str, Any] | str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    lines = [record if isinstance(record, str) else json.dumps(record) for record in records]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "work" / "My App"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def claude_dir(tmp_path: Path) -> Path:
    path = tmp_path / "claude" / "projects"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def exact_dir(claude_dir: Path, project: Path) -> Path:
    return claude_dir / encode_project_path(project)


@pytest.fixture
def index() -> TranscriptIndex:
    return TranscriptIndex()


# --- sessions ---


def test_a_session_reports_its_span_prompt_and_message_count(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    path = write_transcript(
        exact_dir,
        "aaaa-1111",
        [
            {"type": "queue-operation", "timestamp": stamp(0), "operation": "enqueue"},
            user(1, "Build the thing\nwith details on a second line", project),
            assistant(2, project),
            user(9, [{"type": "tool_result", "content": "x"}], project, toolUseResult={"stdout": "x"}),
            assistant(31, project),
            {"type": "ai-title", "title": "no timestamp on this record"},
        ],
    )
    [session] = sessions_for_project(claude_dir, project, index=index)
    assert session == ClaudeSession(
        id="aaaa-1111",
        start=epoch(0),
        end=epoch(31),
        duration_s=31 * 60,
        first_prompt="Build the thing",
        cwd=str(project),
        message_count=4,
        transcript_path=str(path),
    )


def test_timestamps_written_out_of_order_still_bound_the_session(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    write_transcript(exact_dir, "s", [user(10, "late first", project), assistant(2, project), assistant(6, project)])
    [session] = sessions_for_project(claude_dir, project, index=index)
    assert (session.start, session.end) == (epoch(2), epoch(10))


@pytest.mark.parametrize(
    ("skipped_record", "kept_prompt"),
    [
        (user(1, "meta", "c", isMeta=True), "real prompt"),
        (user(1, "This session is being continued from a previous conversation", "c", isCompactSummary=True), "real"),
        (user(1, "<command-name>/model</command-name>", "c"), "real prompt"),
        (user(1, "<local-command-stdout>Set model</local-command-stdout>", "c"), "real prompt"),
        (user(1, "[Request interrupted by user]", "c"), "real prompt"),
        (user(1, "<task-notification>done</task-notification>", "c", origin={"kind": "task-notification"}), "real"),
        (user(1, [{"type": "tool_result", "content": "out"}], "c"), "real prompt"),
        (user(1, "   \n  ", "c"), "real prompt"),
        (user(1, [{"type": "image", "source": {}}], "c"), "real prompt"),
        ({"type": "user", "timestamp": stamp(1), "message": "not an object"}, "real prompt"),
    ],
)
def test_only_a_real_user_prompt_is_the_first_prompt(
    claude_dir: Path,
    exact_dir: Path,
    project: Path,
    index: TranscriptIndex,
    skipped_record: dict[str, Any],
    kept_prompt: str,
) -> None:
    write_transcript(exact_dir, "s", [skipped_record, user(2, kept_prompt, project), assistant(3, project)])
    [session] = sessions_for_project(claude_dir, project, index=index)
    assert session.first_prompt == kept_prompt


def test_prompts_in_block_lists_and_sdk_prompts_count(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    blocks = [{"type": "image", "source": {}}, {"type": "text", "text": "  look at this  "}]
    write_transcript(exact_dir, "blocks", [user(1, blocks, project, origin={"kind": "human"}), assistant(2, project)])
    write_transcript(
        exact_dir, "sdk", [user(3, "You are the builder", project, promptSource="sdk"), assistant(4, project)]
    )
    sessions = {session.id: session.first_prompt for session in sessions_for_project(claude_dir, project, index=index)}
    assert sessions == {"blocks": "look at this", "sdk": "You are the builder"}


def test_a_long_prompt_is_cut_to_one_short_line(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    write_transcript(exact_dir, "s", [user(1, "\n\n" + "word " * 100, project), assistant(2, project)])
    [session] = sessions_for_project(claude_dir, project, index=index)
    assert session.first_prompt is not None
    assert len(session.first_prompt) == MAX_PROMPT_CHARS and session.first_prompt.endswith("…")
    assert "\n" not in session.first_prompt


def test_a_session_without_a_prompt_has_no_first_prompt(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    write_transcript(exact_dir, "s", [assistant(1, project)])
    [session] = sessions_for_project(claude_dir, project, index=index)
    assert session.first_prompt is None and session.message_count == 1


def test_stubs_without_messages_and_empty_files_are_not_sessions(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    write_transcript(exact_dir, "stub", [{"type": "queue-operation", "timestamp": stamp(0)}])
    write_transcript(exact_dir, "empty", [])
    write_transcript(exact_dir, "untimed", [{"type": "user", "message": {"content": "hi"}}])
    assert sessions_for_project(claude_dir, project, index=index) == []


def test_malformed_lines_are_skipped_and_a_half_written_line_waits(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    path = write_transcript(
        exact_dir,
        "live",
        [
            user(1, "first", project),
            "{not json",
            "42",
            '{"type": "user", "timestamp": "yesterday", "message": {"content": "bad stamp"}}',
            assistant(5, project),
        ],
    )
    with path.open("ab") as handle:
        handle.write(b"\xff\xfe broken bytes\n")
        handle.write(json.dumps(assistant(9, project)).encode()[:30])  # the writer is mid-line
    [session] = sessions_for_project(claude_dir, project, index=index)
    assert (session.message_count, session.end, session.first_prompt) == (3, epoch(5), "first")

    with path.open("ab") as handle:
        handle.write(json.dumps(assistant(9, project)).encode()[30:] + b"\n")
    [session] = sessions_for_project(claude_dir, project, index=index)
    assert (session.message_count, session.end) == (4, epoch(9))


def test_sessions_come_latest_activity_first(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    write_transcript(exact_dir, "old", [user(1, "a", project), assistant(2, project)])
    write_transcript(exact_dir, "long-running", [user(0, "b", project), assistant(50, project)])
    write_transcript(exact_dir, "tie", [user(1, "c", project), assistant(2, project)])
    ids = [session.id for session in sessions_for_project(claude_dir, project, index=index)]
    assert ids == ["long-running", "old", "tie"]


def test_missing_projects_dir_means_no_sessions(tmp_path: Path, project: Path, index: TranscriptIndex) -> None:
    assert sessions_for_project(tmp_path / "absent", project, index=index) == []


# --- the lossy encoding ---


def test_directories_that_only_extend_the_name_need_a_cwd_inside_the_project(
    claude_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    sibling = project.parent / "My App-worktrees" / "WP-1"  # encodes to "...My-App-worktrees-WP-1"
    nested = project / "src" / "api"
    elsewhere = project.parent / "Other"
    for cwd in (sibling, nested, elsewhere):
        cwd.mkdir(parents=True)
    write_transcript(claude_dir / encode_project_path(project), "own", [user(1, "own", project), assistant(2, project)])
    write_transcript(
        claude_dir / encode_project_path(sibling), "sibling", [user(1, "sibling", sibling), assistant(2, sibling)]
    )
    write_transcript(
        claude_dir / encode_project_path(nested), "nested", [user(1, "nested", nested), assistant(2, nested)]
    )
    write_transcript(
        claude_dir / (encode_project_path(project) + "-lookalike"),
        "lookalike",
        [user(1, "lookalike", elsewhere), assistant(2, elsewhere)],
    )
    write_transcript(
        claude_dir / (encode_project_path(project) + "-lookalike"),
        "lookalike-again",
        [user(1, "again", elsewhere), assistant(2, elsewhere)],
    )
    write_transcript(
        claude_dir / (encode_project_path(project) + "-nocwd"),
        "nocwd",
        [{"type": "user", "timestamp": stamp(1), "message": {"content": "x"}}],
    )
    write_transcript(
        claude_dir / encode_project_path(elsewhere), "unrelated", [user(1, "u", elsewhere), assistant(2, elsewhere)]
    )
    ids = {session.id for session in sessions_for_project(claude_dir, project, index=index)}
    assert ids == {"own", "nested"}


def test_a_name_that_merely_starts_with_the_same_letters_is_another_project(
    claude_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    neighbour = project.parent / "My Application"
    neighbour.mkdir()
    write_transcript(
        claude_dir / encode_project_path(neighbour), "neighbour", [user(1, "n", neighbour), assistant(2, neighbour)]
    )
    assert sessions_for_project(claude_dir, project, index=index) == []


def test_files_that_are_not_transcripts_are_ignored(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    write_transcript(exact_dir, "real", [user(1, "x", project), assistant(2, project)])
    (exact_dir / "notes.txt").write_text("not a transcript", encoding="utf-8")
    (exact_dir / "real" / "subagents").mkdir(parents=True)
    write_transcript(exact_dir / "real" / "subagents", "agent-1", [user(1, "sub", project), assistant(2, project)])
    (claude_dir / "stray-file.jsonl").write_text("{}\n", encoding="utf-8")
    assert [session.id for session in sessions_for_project(claude_dir, project, index=index)] == ["real"]


# --- caching ---


@contextmanager
def recorded_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """The byte offset each transcript read resumed from (0 for a first read)."""
    resumed_from: list[int] = []
    original: Callable[..., Any] = TranscriptIndex._read_new_records

    def spy(path: Path, size: int, known: Any) -> Any:
        resumed_from.append(known.offset if known is not None else 0)
        return original(path, size, known)

    monkeypatch.setattr(TranscriptIndex, "_read_new_records", staticmethod(spy))
    yield resumed_from


def test_an_unchanged_transcript_is_not_read_again(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_transcript(exact_dir, "s", [user(1, "x", project), assistant(2, project)])
    with recorded_reads(monkeypatch) as reads:
        first = sessions_for_project(claude_dir, project, index=index)
        second = sessions_for_project(claude_dir, project, index=index)
    assert reads == [0] and second[0] is first[0]


def test_a_growing_transcript_is_parsed_from_where_it_stopped(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_transcript(exact_dir, "s", [user(1, "x", project), assistant(2, project)])
    with recorded_reads(monkeypatch) as reads:
        [before] = sessions_for_project(claude_dir, project, index=index)
        size_before = path.stat().st_size
        with path.open("ab") as handle:
            handle.write(json.dumps(assistant(20, project)).encode() + b"\n")
        [after] = sessions_for_project(claude_dir, project, index=index)
    assert reads == [0, size_before]
    assert (before.message_count, after.message_count) == (2, 3)
    assert (after.start, after.end, after.first_prompt) == (before.start, epoch(20), "x")


def test_a_rewritten_transcript_is_parsed_from_the_start(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    write_transcript(
        exact_dir, "s", [user(1, "original prompt", project), assistant(2, project), assistant(3, project)]
    )
    [before] = sessions_for_project(claude_dir, project, index=index)
    assert before.message_count == 3
    write_transcript(
        exact_dir,
        "s",
        [user(5, "a different, much longer rewritten prompt", project), assistant(6, project), assistant(7, project)]
        + [assistant(8, project)] * 3,
    )
    [after] = sessions_for_project(claude_dir, project, index=index)
    assert (after.first_prompt, after.message_count, after.start) == (
        "a different, much longer rewritten prompt",
        6,
        epoch(5),
    )


def test_a_truncated_transcript_is_parsed_from_the_start(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    write_transcript(exact_dir, "s", [user(1, "x", project), assistant(2, project), assistant(3, project)])
    sessions_for_project(claude_dir, project, index=index)
    write_transcript(exact_dir, "s", [user(1, "x", project)])
    [after] = sessions_for_project(claude_dir, project, index=index)
    assert after.message_count == 1


def test_a_transcript_deleted_between_listing_and_reading_is_dropped(
    claude_dir: Path, exact_dir: Path, project: Path, index: TranscriptIndex
) -> None:
    path = write_transcript(exact_dir, "s", [user(1, "x", project), assistant(2, project)])
    assert index.session(path) is not None
    path.unlink()
    assert index.session(path) is None and index.starting_cwd(path) is None


def test_the_starting_cwd_is_found_once_and_remembered(
    exact_dir: Path, project: Path, index: TranscriptIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_transcript(exact_dir, "s", [{"type": "queue-operation"}, user(1, "x", project)])
    assert index.starting_cwd(path) == str(project)
    monkeypatch.setattr(claude_sessions, "_peek_cwd", lambda _path: pytest.fail("a known cwd must not be re-read"))
    assert index.starting_cwd(path) == str(project)


def test_a_transcript_without_a_complete_cwd_line_has_no_starting_cwd(exact_dir: Path, index: TranscriptIndex) -> None:
    path = write_transcript(exact_dir, "s", [{"type": "queue-operation"}])
    assert index.starting_cwd(path) is None
    with path.open("ab") as handle:
        handle.write(b'{"type": "user", "cwd": "half')
    assert index.starting_cwd(path) is None


# --- processes ---


@pytest.mark.parametrize(
    ("name", "cmdline", "expected"),
    [
        ("claude.exe", [], True),
        ("Claude.EXE", ["claude.exe", "-p", "hi"], True),
        ("claude", [], True),
        ("node.exe", ["node", r"C:\npm\node_modules\@anthropic-ai\claude-code\cli.js"], True),
        ("node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "--resume"], True),
        ("bun", ["bun", "/x/@anthropic-ai/claude-code/cli.js"], True),
        ("node.exe", ["node", "server.js"], False),
        ("node.exe", ["node", "npx-cli.js", "@agentclientprotocol/claude-agent-acp@latest"], False),
        ("python.exe", ["python", "claude"], False),
        ("claude-helper.exe", [], False),
    ],
)
def test_claude_code_is_recognised_by_executable_or_package(name: str, cmdline: list[str], expected: bool) -> None:
    assert is_claude_process(name, cmdline) is expected


def test_secrets_never_survive_in_a_command_line() -> None:
    long_prompt = "x" * 500
    redacted = redact_cmdline(
        [
            "claude",
            "--api-key",
            "sk-secret",
            "--auth-token=abc",
            "--max-thinking-tokens",
            "100",
            "-p",
            long_prompt,
            "--client-secret",
            "s3",
            "--model",
            "opus",
        ]
    )
    assert redacted == [
        "claude",
        "--api-key",
        "--auth-token",
        "--max-thinking-tokens",
        "100",
        "-p",
        "x" * (MAX_ARGUMENT_CHARS - 1) + "…",
        "--client-secret",
        "--model",
        "opus",
    ]


class FakeProcess:
    """The slice of `psutil.Process` that process scanning touches."""

    def __init__(
        self, pid: int, name: str, cmdline: list[str], cwd: str | Exception, create_time: float | Exception = 1000.0
    ) -> None:
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._cmdline = cmdline
        self._cwd = cwd
        self._create_time = create_time
        self._name = name

    @contextmanager
    def oneshot(self) -> Iterator[None]:
        yield

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return self._cmdline

    def create_time(self) -> float:
        if isinstance(self._create_time, Exception):
            raise self._create_time
        return self._create_time

    def cwd(self) -> str:
        if isinstance(self._cwd, Exception):
            raise self._cwd
        return self._cwd


@pytest.fixture
def process_table(monkeypatch: pytest.MonkeyPatch, project: Path) -> list[FakeProcess]:
    table = [
        FakeProcess(30, "claude.exe", ["claude.exe", "--api-key", "sk-1", "-p", "go"], str(project / "src"), 2000.0),
        FakeProcess(10, "claude.exe", ["claude.exe"], str(project.parent / "Other")),
        FakeProcess(20, "node.exe", ["node", "x/@anthropic-ai/claude-code/cli.js"], str(project)),
        FakeProcess(40, "node.exe", ["node", "dev-server.js"], str(project)),
        FakeProcess(50, "claude.exe", ["claude.exe"], psutil.AccessDenied(pid=50)),
        FakeProcess(60, "claude.exe", ["claude.exe"], str(project), psutil.NoSuchProcess(pid=60)),
        FakeProcess(70, "claude.exe", ["claude.exe"], OSError("only part of a ReadProcessMemory")),
        FakeProcess(80, "python.exe", ["python"], str(project)),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs=None: iter(table))
    return table


def test_running_processes_are_listed_by_pid_without_secrets_or_unreadable_ones(
    process_table: list[FakeProcess], project: Path
) -> None:
    processes = running_claude_processes()
    assert [process.pid for process in processes] == [10, 20, 30]
    thirty = processes[2]
    assert (thirty.started_at, thirty.cwd, thirty.cmdline) == (
        2000.0,
        str(project / "src"),
        ["claude.exe", "--api-key", "-p", "go"],
    )


def test_active_processes_are_those_whose_cwd_is_inside_the_project(
    process_table: list[FakeProcess], project: Path
) -> None:
    assert [process.pid for process in active_claude_processes(project)] == [20, 30]
    assert [process.pid for process in processes_inside(running_claude_processes(), project / "src")] == [30]
    assert active_claude_processes(project.parent / "nothing-here") == []


def test_scanning_the_live_process_table_returns_well_formed_processes() -> None:
    assert all(isinstance(process, ClaudeProcess) and process.pid > 0 for process in running_claude_processes())


# --- the overview's facts ---


def test_agent_facts_summarise_sessions_and_processes(project: Path) -> None:
    session = ClaudeSession(
        id="s", start=1.0, end=50.0, duration_s=49.0, first_prompt=None, cwd=None, message_count=2, transcript_path="t"
    )
    later = session.model_copy(update={"id": "later", "end": 90.0})
    running = ClaudeProcess(pid=1, started_at=1.0, cwd=str(project))
    facts = agent_facts([session, later], [running])
    assert (facts.active, facts.last_session_at, facts.session_count) == (True, 90.0, 2)
    quiet = agent_facts([], [])
    assert (quiet.active, quiet.last_session_at, quiet.session_count) == (False, None, 0)
