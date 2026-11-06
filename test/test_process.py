"""Process-tree ownership and port allocation against real processes and sockets."""

import asyncio
import contextlib
import socket
import sys
import time
from pathlib import Path

import psutil
import pytest

from forge_dashboard.process import ports, tree

PY = sys.executable


async def test_run_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = await tree.run([PY, "-c", script], tmp_path)
    assert (result.exit_code, result.stdout.strip(), result.stderr.strip(), result.ok) == (3, "out", "err", False)


async def test_run_feeds_stdin(tmp_path: Path) -> None:
    result = await tree.run([PY, "-c", "import sys; print(sys.stdin.read().upper())"], tmp_path, stdin=b"hello")
    assert result.stdout.strip() == "HELLO"


async def test_run_timeout_kills_and_reports(tmp_path: Path) -> None:
    result = await tree.run([PY, "-c", "import time; time.sleep(30)"], tmp_path, timeout_s=0.5)
    assert result.timed_out and result.exit_code is None and not result.ok


async def test_stream_delivers_lines_as_they_arrive(tmp_path: Path) -> None:
    seen: list[str] = []
    result = await tree.stream([PY, "-c", "print('a'); print('b')"], tmp_path, None, seen.append)
    assert seen == ["a", "b"]
    assert result.ok and result.stdout == "a\nb"


async def test_missing_executable_raises_a_named_error(tmp_path: Path) -> None:
    with pytest.raises(tree.ExecutableNotFoundError, match="definitely-not-installed"):
        await tree.run(["definitely-not-installed-xyz"], tmp_path)


async def test_stop_takes_down_grandchildren(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    owned = await tree.spawn([PY, "-c", script], tmp_path)
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    grandchild = int(pid_file.read_text())
    assert grandchild in owned.tree_pids()
    stages: list[str] = []
    await owned.stop(grace_s=2, on_stage=stages.append)
    assert not owned.is_alive()
    assert not psutil.pid_exists(grandchild) or psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE
    assert stages


async def test_cancelled_run_kills_its_tree(tmp_path: Path) -> None:
    task = asyncio.create_task(tree.run([PY, "-c", "import time; time.sleep(30)"], tmp_path))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_allocation_skips_a_preferred_port_with_a_wildcard_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("0.0.0.0", 0))
        holder.listen()
        taken = holder.getsockname()[1]
        assert ports.port_has_listener(taken)
        assert not ports.port_is_free(taken)
        assert ports.allocate_port(taken) != taken


def test_allocation_honours_reserved_ports() -> None:
    free = ports.allocate_port(None)
    assert ports.allocate_port(free, reserved={free}) != free
    assert ports.allocate_port(free) == free


def test_holders_identify_the_listening_process() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        [found] = ports.holders([port])
    assert found.pid == psutil.Process().pid
    assert found.lives_inside(Path(sys.executable).parent.parent) or found.cwd is not None


def test_protected_pids_include_us_and_system_processes() -> None:
    protected = ports.protected_pids()
    assert {0, 4, psutil.Process().pid} <= protected


def test_reclaim_refuses_protected_processes() -> None:
    me = psutil.Process()
    holder = ports.PortHolder(port=1, pid=me.pid, name=me.name(), cmdline=(), cwd=None, create_time=me.create_time())
    with pytest.raises(PermissionError):
        ports.reclaim(holder)


async def test_reclaim_kills_a_leftover_listener(tmp_path: Path) -> None:
    script = (
        "import socket, time\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "print(s.getsockname()[1], flush=True)\n"
        "time.sleep(60)\n"
    )
    process = await asyncio.create_subprocess_exec(PY, "-c", script, cwd=tmp_path, stdout=asyncio.subprocess.PIPE)
    assert process.stdout is not None
    port = int((await process.stdout.readline()).decode())
    [holder] = ports.holders([port])
    assert holder.lives_inside(tmp_path)
    assert await asyncio.to_thread(ports.reclaim, holder)
    await process.wait()


def test_path_is_within_rejects_unrelated_paths(tmp_path: Path) -> None:
    assert ports.path_is_within(str(tmp_path / "a" / "b"), tmp_path)
    assert not ports.path_is_within(str(tmp_path.parent), tmp_path)
    assert not ports.path_is_within("\0bad", tmp_path)


def test_relative_fragments_never_count_as_inside_a_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert not ports.path_is_within("-k", tmp_path)
    assert not ports.path_is_within("RPCSS", tmp_path)
    holder = ports.PortHolder(
        port=1, pid=1, name="svchost.exe", cmdline=("svchost.exe", "-k", "RPCSS"), cwd=None, create_time=0.0
    )
    assert not holder.lives_inside(tmp_path)


def test_detached_launches_outlive_no_job_and_report_their_pid(tmp_path: Path) -> None:
    marker = tmp_path / "launched.txt"
    pid = tree.launch_detached([PY, "-c", f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ok')"], tmp_path)
    assert pid > 0
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.read_text(encoding="utf-8") == "ok"
    with pytest.raises(tree.ExecutableNotFoundError):
        tree.launch_detached(["definitely-not-installed-xyz"], tmp_path)


STUBBORN_CHILD = (
    "import signal, time\n"
    "for name in ('SIGTERM', 'SIGBREAK'):\n"
    "    if hasattr(signal, name):\n"
    "        signal.signal(getattr(signal, name), signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


async def test_cancelling_a_stop_still_force_kills_a_child_that_ignores_the_stop_signal(tmp_path: Path) -> None:
    owned = await tree.spawn([PY, "-c", STUBBORN_CHILD], tmp_path)
    stages: list[str] = []
    try:
        assert owned.process.stdout is not None
        assert (await owned.process.stdout.readline()).strip() == b"ready"
        stopping = asyncio.create_task(owned.stop(grace_s=30, on_stage=stages.append))
        while not stages:
            await asyncio.sleep(0.02)
        assert owned.is_alive()  # the child is ignoring the graceful signal, inside the 30s grace period
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert not owned.is_alive()
    finally:
        await owned.kill()


class RecycledProcess:
    """A snapshot of a process whose pid has since been reused: psutil no longer recognises it as running."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.killed = False

    def is_running(self) -> bool:
        return False

    def status(self) -> str:
        return psutil.STATUS_RUNNING

    def kill(self) -> None:
        self.killed = True


async def test_force_kill_spares_the_stranger_that_now_owns_a_recorded_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stranger = await asyncio.create_subprocess_exec(PY, "-c", "import time; time.sleep(60)")
    owned = await tree.spawn([PY, "-c", "import time; time.sleep(60)"], tmp_path)
    try:
        recycled = RecycledProcess(stranger.pid)
        genuine = owned._tree_processes()
        monkeypatch.setattr(owned, "_tree_processes", lambda: {**genuine, stranger.pid: recycled})
        await owned.stop(grace_s=2)
        assert not owned.is_alive()
        assert not recycled.killed
        assert psutil.Process(stranger.pid).is_running()
    finally:
        stranger.kill()
        await stranger.wait()
        await owned.kill()


async def test_a_process_tree_snapshot_keeps_the_identity_of_every_member(tmp_path: Path) -> None:
    owned = await tree.spawn([PY, "-c", "import time; time.sleep(60)"], tmp_path)
    try:
        snapshot = owned._tree_processes()
        assert snapshot[owned.pid].create_time() == psutil.Process(owned.pid).create_time()
        assert owned.tree_pids() == set(snapshot)
    finally:
        await owned.kill()


def _denied(*_args: object, **_kwargs: object) -> None:
    raise psutil.AccessDenied(0)


def test_listeners_fall_back_to_the_processes_we_may_inspect_when_the_system_table_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        monkeypatch.setattr(psutil, "net_connections", _denied)
        mine = [listener for listener in ports.listeners() if listener.port == port]
        assert [listener.pid for listener in mine] == [psutil.Process().pid]
        assert ports.port_has_listener(port)
        assert [found.pid for found in ports.holders([port])] == [psutil.Process().pid]


def test_a_connect_probe_covers_listeners_of_processes_we_cannot_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil, "net_connections", _denied)
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter(()))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        busy = holder.getsockname()[1]
        assert ports.listeners() == []
        assert ports.port_has_listener(busy)
    assert not ports.port_has_listener(busy)


def test_a_holder_the_os_refuses_to_describe_is_unverifiable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Denied:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def oneshot(self) -> contextlib.AbstractContextManager[None]:
            return contextlib.nullcontext()

        def cwd(self) -> str:
            raise psutil.AccessDenied(self.pid)

        def cmdline(self) -> list[str]:
            raise psutil.AccessDenied(self.pid)

        def name(self) -> str:
            raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil, "Process", Denied)
    holder = ports.describe(4242, 8080)
    assert holder is not None and holder.name == "?" and not holder.verifiable


def test_reclaim_refuses_a_holder_whose_identity_cannot_be_verified() -> None:
    unverifiable = ports.PortHolder(
        port=8080,
        pid=2_000_000_000,
        name="?",
        cmdline=(),
        cwd=None,
        create_time=ports.UNVERIFIABLE_CREATE_TIME,
    )
    with pytest.raises(PermissionError, match="cannot verify process 2000000000"):
        ports.reclaim(unverifiable)


def test_create_times_name_only_processes_that_are_running() -> None:
    me = psutil.Process()
    assert ports.create_times([me.pid, 2_000_000_000]) == {me.pid: me.create_time()}
    assert ports.still_running({me.pid: me.create_time(), 2_000_000_000: 1.0}) == {me.pid: me.create_time()}
    assert ports.still_running({me.pid: me.create_time() - 5}) == {}
