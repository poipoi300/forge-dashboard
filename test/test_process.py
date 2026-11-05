"""Process-tree ownership and port allocation against real processes and sockets."""

import asyncio
import socket
import sys
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
