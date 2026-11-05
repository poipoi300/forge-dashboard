"""The one place that starts operating-system processes.

Every subprocess the dashboard runs, from a one-shot `git status` to a
supervised dev server, goes through this module so that each one owns its
whole process tree. Package-manager wrappers (`uv run`, `bun run`) exit and
leave the real server behind, so a PID alone is not enough:

- Windows: the child starts suspended in a new process group, is assigned to
  a kill-on-close Job Object, then resumed. CTRL_BREAK reaches the group for a
  graceful stop; the job kills every member on force or on dashboard exit.
- POSIX: the child starts a new session, and the whole group is signalled.

Stops are staged: a graceful signal, a grace period, then a forced kill of
everything still standing, re-enumerating the tree after the graceful phase.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psutil

if sys.platform == "win32":
    from forge_dashboard.process.winjob import JobObject

IS_WINDOWS = sys.platform == "win32"
DEFAULT_GRACE_S = 8.0
# Win32 CREATE_SUSPENDED; the subprocess module does not export it.
_CREATE_SUSPENDED = 0x00000004

StageReporter = Callable[[str], None]


class ExecutableNotFoundError(FileNotFoundError):
    """argv[0] does not resolve on PATH."""

    def __init__(self, executable: str) -> None:
        super().__init__(f"{executable!r} is not on PATH")
        self.executable = executable


def resolve_executable(executable: str, env: Mapping[str, str] | None = None) -> str:
    """Resolve argv[0] on PATH, honouring PATHEXT on Windows.

    Raises:
        ExecutableNotFoundError: when nothing on PATH matches.
    """
    search_path = (env or os.environ).get("PATH")
    resolved = shutil.which(executable, path=search_path)
    if resolved is None:
        raise ExecutableNotFoundError(executable)
    return resolved


def child_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a child: ours, unbuffered Python output, then overrides."""
    return {**os.environ, "PYTHONUNBUFFERED": "1", **(overrides or {})}


@dataclass
class OwnedProcess:
    """A running child that owns its whole tree."""

    process: asyncio.subprocess.Process
    argv: tuple[str, ...]
    started_monotonic: float = field(default_factory=time.monotonic)
    _job: object | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def is_alive(self) -> bool:
        return self.process.returncode is None

    def tree_pids(self) -> set[int]:
        """PIDs of the child and every live descendant, including orphaned grandchildren on Windows."""
        pids: set[int] = set()
        if sys.platform == "win32" and isinstance(self._job, JobObject):
            with contextlib.suppress(OSError, RuntimeError):
                pids |= self._job.pids()
        with contextlib.suppress(psutil.Error):
            root = psutil.Process(self.pid)
            pids.add(root.pid)
            pids |= {child.pid for child in root.children(recursive=True)}
        return pids

    async def lines(self) -> AsyncIterator[str]:
        """Yield merged output lines until the child closes its stream; ANSI colours are kept."""
        stream = self.process.stdout
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                return
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")

    async def wait(self) -> int:
        code = await self.process.wait()
        self._release_job()
        return code

    async def stop(self, grace_s: float = DEFAULT_GRACE_S, on_stage: StageReporter | None = None) -> None:
        """Stop the tree: graceful signal, wait, re-enumerate, force-kill survivors."""
        report = on_stage or (lambda _message: None)
        known = self.tree_pids()
        if not self._survivors(known) and not self._group_alive():
            self._release_job()
            return
        if _send_graceful(self.pid):
            report(f"sent a graceful stop signal; waiting up to {grace_s:.0f}s")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=grace_s)
        known |= self.tree_pids()
        if not self._survivors(known) and not self._group_alive():
            report("stopped cleanly")
            self._release_job()
            return
        report("graceful stop did not finish in time; killing the process tree")
        await self._force_kill(known, grace_s)
        report("process tree killed" if not self._survivors(known) and not self.is_alive() else "kill not confirmed")

    async def kill(self) -> None:
        """Kill the tree immediately, without the graceful phase."""
        await self._force_kill(self.tree_pids(), DEFAULT_GRACE_S)

    async def _force_kill(self, known: set[int], grace_s: float) -> None:
        if sys.platform == "win32" and isinstance(self._job, JobObject):
            with contextlib.suppress(OSError, RuntimeError):
                self._job.terminate()
        if sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signal.SIGKILL)
        survivors = self._survivors(known)
        for process in survivors:
            with contextlib.suppress(psutil.Error):
                process.kill()
        await asyncio.to_thread(psutil.wait_procs, survivors, timeout=grace_s)
        if self.is_alive():
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.process.wait(), timeout=grace_s)
        self._release_job()

    def _group_alive(self) -> bool:
        """POSIX: whether any member of the child's process group survives (orphans included).

        On Windows the Job Object membership already covers orphans via `tree_pids`.
        """
        if sys.platform == "win32":
            return False
        try:
            os.killpg(self.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _survivors(self, known: set[int]) -> list[psutil.Process]:
        alive: list[psutil.Process] = []
        for pid in known | ({self.pid} if self.is_alive() else set()):
            with contextlib.suppress(psutil.Error):
                process = psutil.Process(pid)
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process)
        return alive

    def _release_job(self) -> None:
        if sys.platform == "win32" and isinstance(self._job, JobObject):
            self._job.close()
        self._job = None


def _send_graceful(pid: int) -> bool:
    """Deliver the platform's polite stop: CTRL_BREAK to the group on Windows, SIGTERM to the group on POSIX."""
    try:
        if sys.platform == "win32":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return False
    return True


async def spawn(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None = None,
    *,
    stdin: bytes | None = None,
    merge_stderr: bool = True,
) -> OwnedProcess:
    """Start `argv` owning its tree.

    Cancellation-safe: if the caller is cancelled while the child is being
    created, the child is still created, then killed, so nothing leaks.

    Args:
        argv: Command; argv[0] is resolved on PATH (with PATHEXT on Windows).
        cwd: Working directory.
        env: Full child environment (see `child_env`); None inherits ours.
        stdin: Bytes written to the child's stdin before it is closed; None
            gives the child an empty stdin.
        merge_stderr: Merge stderr into stdout (logs) or keep it separate.

    Raises:
        ExecutableNotFoundError: when argv[0] does not resolve.
    """
    creation: asyncio.Task[OwnedProcess] = asyncio.create_task(
        _spawn_owned(argv, cwd, env, stdin=stdin, merge_stderr=merge_stderr)
    )
    try:
        return await asyncio.shield(creation)
    except asyncio.CancelledError:
        owned = await creation
        await owned.kill()
        raise


async def _spawn_owned(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None,
    *,
    stdin: bytes | None,
    merge_stderr: bool,
) -> OwnedProcess:
    executable = resolve_executable(argv[0], env)
    command = (executable, *argv[1:])
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=dict(env) if env is not None else None,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        creationflags=creation_flags,
        start_new_session=sys.platform != "win32",
    )
    owned = OwnedProcess(process=process, argv=tuple(argv))
    if sys.platform == "win32":
        job = JobObject()
        try:
            job.assign(process.pid)
        except OSError:
            job.close()
            psutil.Process(process.pid).resume()
            await owned.kill()
            raise
        owned._job = job
        psutil.Process(process.pid).resume()
    if stdin is not None and process.stdin is not None:
        process.stdin.write(stdin)
        with contextlib.suppress(ConnectionError):
            await process.stdin.drain()
        process.stdin.close()
    return owned


@dataclass(frozen=True)
class CompletedRun:
    """Outcome of a run-to-completion command."""

    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


async def run(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None = None,
    *,
    timeout_s: float | None = None,
    stdin: bytes | None = None,
) -> CompletedRun:
    """Run a command to completion with separate stdout and stderr.

    A timeout kills the whole tree and is reported, not raised. Cancellation
    kills the tree and re-raises.

    Raises:
        ExecutableNotFoundError: when argv[0] does not resolve.
    """
    started = time.monotonic()
    owned = await spawn(argv, cwd, env, stdin=stdin, merge_stderr=False)
    communicate = asyncio.ensure_future(_read_both(owned.process))
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        await owned.kill()
        stdout, stderr = await communicate
    except asyncio.CancelledError:
        await owned.kill()
        raise
    code = await owned.wait()
    return CompletedRun(
        argv=tuple(argv),
        exit_code=None if timed_out else code,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
    )


async def _read_both(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    async def drain(stream: asyncio.StreamReader | None) -> bytes:
        return await stream.read() if stream is not None else b""

    stdout, stderr = await asyncio.gather(drain(process.stdout), drain(process.stderr))
    return stdout, stderr


async def stream(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None,
    on_line: Callable[[str], None],
    *,
    timeout_s: float | None = None,
    stdin: bytes | None = None,
) -> CompletedRun:
    """Run a command to completion, delivering merged output lines as they arrive.

    The returned `stdout` holds the full merged output; `stderr` is empty.
    """
    started = time.monotonic()
    owned = await spawn(argv, cwd, env, stdin=stdin, merge_stderr=True)
    collected: list[str] = []

    async def pump() -> None:
        async for line in owned.lines():
            collected.append(line)
            on_line(line)

    timed_out = False
    try:
        await asyncio.wait_for(pump(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        await owned.kill()
    except asyncio.CancelledError:
        await owned.kill()
        raise
    code = await owned.wait()
    return CompletedRun(
        argv=tuple(argv),
        exit_code=None if timed_out else code,
        stdout="\n".join(collected),
        stderr="",
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
    )
