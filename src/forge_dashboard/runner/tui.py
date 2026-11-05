"""The runner's terminal UI: the supervisor's services on the left, the selected one's log on the right.

Rendering only: every decision lives in the supervisor. One shared 1-second
tick recomputes every uptime and countdown in the same frame. The default
theme is untouched; colour only encodes state, and every state also reads as
a word.
"""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from forge_dashboard.supervisor.core import ServiceStatus, Supervisor
from forge_dashboard.supervisor.policy import ServiceState

STATE_STYLE: dict[ServiceState, str] = {
    ServiceState.RUNNING: "green",
    ServiceState.STARTING: "yellow",
    ServiceState.BUILDING: "yellow",
    ServiceState.BACKOFF: "yellow",
    ServiceState.STOPPING: "yellow",
    ServiceState.FAILED: "red",
    ServiceState.BUILD_FAILED: "red",
    ServiceState.BLOCKED: "red",
    ServiceState.STOPPED: "dim",
}
COLUMNS = ("service", "state", "pid", "port", "uptime", "↻")
NARROW_DROPS = ("pid",)
STACK_BELOW_LOG_COLUMNS = 40
LOG_LINES = 2000


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def row_cells(status: ServiceStatus, now: float, columns: tuple[str, ...]) -> list[str | Text]:
    """One table row, a pure function of the snapshot, the shared tick, and the visible columns."""
    started = status.ready_at or status.started_at
    uptime = format_duration(now - started) if started is not None and status.state is ServiceState.RUNNING else ""
    values: dict[str, str | Text] = {
        "service": status.name,
        "state": Text(status.state.value, style=STATE_STYLE[status.state]),
        "pid": str(status.pid) if status.pid is not None else "",
        "port": str(status.port) if status.port is not None else "",
        "uptime": uptime,
        "↻": str(status.restarts) if status.restarts else "",
    }
    return [values[column] for column in columns]


def detail_line(status: ServiceStatus, now: float) -> str:
    """The selected service's one-line detail: description, countdown or URL, and the supervisor's note."""
    parts = [status.description]
    if status.state is ServiceState.BACKOFF and status.retry_at is not None:
        parts.append(f"retry in {format_duration(max(0.0, status.retry_at - now))}")
    if status.url is not None:
        parts.append(status.url)
    if status.detail and status.state is not ServiceState.RUNNING:
        parts.append(status.detail)
    return " · ".join(part for part in parts if part)


class HelpScreen(ModalScreen[None]):
    """Every binding, generated from the app's binding table so it cannot drift."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,question_mark,q", "dismiss", "close")]

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > DataTable { width: auto; height: auto; max-height: 80%; }
    """

    def __init__(self, entries: list[Binding]) -> None:
        super().__init__()
        self._entries = entries

    def compose(self) -> ComposeResult:
        table: DataTable[str] = DataTable(show_cursor=False)
        table.add_columns("key", "action")
        for binding in self._entries:
            table.add_row(binding.key_display or binding.key, binding.description)
        yield table


class RunnerApp(App[None]):
    """forge dashboard runner."""

    TITLE = "forge dashboard"
    CSS = """
    #body { height: 1fr; }
    #services { width: auto; min-width: 44; }
    #side { width: 1fr; }
    #detail { height: 1; padding: 0 1; }
    #log { height: 1fr; }
    #body.stacked { layout: vertical; }
    #body.stacked #services { width: 1fr; height: auto; max-height: 50%; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("o", "open", "open"),
        Binding("r", "restart", "restart"),
        Binding("l", "write_log", "logs"),
        Binding("x", "stop", "stop"),
        Binding("s", "start", "start"),
        Binding("a", "start_all", "start all", show=False),
        Binding("z", "stop_all", "stop all", show=False),
        Binding("question_mark", "help", "help", key_display="?"),
        Binding("q", "quit_runner", "quit", priority=True),
        Binding("ctrl+c", "quit_runner", "quit", show=False, priority=True),
        Binding("ctrl+q", "quit_runner", "quit", show=False, priority=True),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        log_dir: Path,
        autostart: bool = True,
        open_when_ready: str | None = None,
        open_url: Callable[[str], object] = webbrowser.open,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self.supervisor = supervisor
        self._log_dir = log_dir
        self._autostart = autostart
        self._open_when_ready = open_when_ready
        self._open_url = open_url
        self._clock = clock
        self._opened = False
        self._shown_lines = 0
        self._columns: tuple[str, ...] = COLUMNS
        self._names = list(supervisor.services)
        self._mounted = False

    # --- layout ---

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            table: DataTable[str | Text] = DataTable(id="services", cursor_type="row", zebra_stripes=False)
            yield table
            with Vertical(id="side"):
                yield Static("", id="detail")
                yield RichLog(id="log", max_lines=LOG_LINES, wrap=False, markup=False, highlight=False)
        yield Footer(show_command_palette=False, compact=True)

    def on_mount(self) -> None:
        self._mounted = True
        self._rebuild_columns()
        self.set_interval(1.0, self._tick)
        self.supervisor_changed()
        if self._autostart:
            self.run_worker(self.supervisor.start_all(), exclusive=False)

    def on_resize(self) -> None:
        self._rebuild_columns()

    def _rebuild_columns(self) -> None:
        width = self.size.width
        columns = COLUMNS if width >= 100 else tuple(c for c in COLUMNS if c not in NARROW_DROPS)
        body = self.query_one("#body")
        body.set_class(width - 46 < STACK_BELOW_LOG_COLUMNS, "stacked")
        if columns == self._columns and self.query_one(DataTable).columns:
            return
        self._columns = columns
        table = self.query_one(DataTable)
        table.clear(columns=True)
        for column in columns:
            table.add_column(column, key=column)
        for name in self._names:
            table.add_row(*row_cells(self.supervisor.status(name), self._clock(), columns), key=name)

    # --- refresh ---

    def supervisor_changed(self) -> None:
        """Called by the supervisor on every change (from the same event loop)."""
        if not self._mounted:
            return
        self._render_rows()
        self._render_side(new_lines_only=True)
        self._maybe_open()

    def _tick(self) -> None:
        self._render_rows()
        self._render_side(new_lines_only=True)

    def _render_rows(self) -> None:
        table = self.query_one(DataTable)
        now = self._clock()
        for name in self._names:
            cells = row_cells(self.supervisor.status(name), now, self._columns)
            for column, value in zip(self._columns, cells, strict=True):
                table.update_cell(name, column, value)

    def _selected(self) -> str:
        table = self.query_one(DataTable)
        row = table.cursor_row if table.row_count else 0
        return self._names[min(max(row, 0), len(self._names) - 1)]

    def _render_side(self, *, new_lines_only: bool) -> None:
        name = self._selected()
        status = self.supervisor.status(name)
        self.query_one("#detail", Static).update(detail_line(status, self._clock()))
        log = self.query_one(RichLog)
        lines = self.supervisor.log_lines(name)
        if not new_lines_only or len(lines) < self._shown_lines:
            log.clear()
            self._shown_lines = 0
        for line in lines[self._shown_lines :]:
            log.write(Text.from_ansi(line))
        self._shown_lines = len(lines)

    @on(DataTable.RowHighlighted)
    def _row_changed(self) -> None:
        self._shown_lines = 0
        self._render_side(new_lines_only=False)

    def _maybe_open(self) -> None:
        if self._opened or self._open_when_ready is None:
            return
        status = self.supervisor.status(self._open_when_ready)
        if status.state is ServiceState.RUNNING and status.url is not None:
            self._opened = True
            self._open_url(status.url)

    # --- actions ---

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_open(self) -> None:
        status = self.supervisor.status(self._selected())
        if status.url is None:
            self.notify(f"{status.name} has no URL while {status.state.value}", severity="warning")
            return
        self._open_url(status.url)

    def action_start(self) -> None:
        self.run_worker(self.supervisor.start(self._selected()))

    def action_stop(self) -> None:
        self.run_worker(self.supervisor.stop(self._selected()))

    def action_restart(self) -> None:
        self.run_worker(self.supervisor.restart(self._selected()))

    def action_start_all(self) -> None:
        self.run_worker(self.supervisor.start_all())

    def action_stop_all(self) -> None:
        self.run_worker(self.supervisor.stop_all())

    def action_write_log(self) -> None:
        path = self.supervisor.write_log(self._selected(), self._log_dir)
        self.notify(f"wrote {path}")

    def action_help(self) -> None:
        bindings = [binding for binding in self.BINDINGS if isinstance(binding, Binding)]
        self.push_screen(HelpScreen(bindings))

    @work(exclusive=True, group="quit")
    async def action_quit_runner(self) -> None:
        self.query_one("#detail", Static).update("stopping services…")
        await self.supervisor.stop_all()
        self.exit()
