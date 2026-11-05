"""Port freeness, allocation, and listener ownership.

A port is free only when the OS listener table shows no listener on it, on any
address or IP family. A bind probe alone is not proof: on Windows a loopback
bind succeeds next to another process's `0.0.0.0` listener, and the new
service then silently shadows it. Allocation therefore checks the table first
and confirms with a wildcard bind (never with SO_REUSEADDR).
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

import psutil

# PIDs that must never be killed to free a port: the System Idle Process and System on Windows.
_SYSTEM_PIDS = frozenset({0, 4})


@dataclass(frozen=True)
class Listener:
    """One TCP listening socket and the process behind it."""

    port: int
    address: str
    pid: int | None


@dataclass(frozen=True)
class PortHolder:
    """A process listening on a port, with what identifies it."""

    port: int
    pid: int
    name: str
    cmdline: tuple[str, ...]
    cwd: str | None
    create_time: float

    def lives_inside(self, root: Path) -> bool:
        """Whether the holder's cwd or any cmdline path lies inside `root`."""
        candidates = [self.cwd, *self.cmdline] if self.cwd else list(self.cmdline)
        return any(path_is_within(candidate, root) for candidate in candidates if candidate)


def path_is_within(candidate: str, root: Path) -> bool:
    """Case-insensitive on Windows; tolerant of quoted or relative fragments, which never match."""
    try:
        resolved = Path(candidate).resolve()
    except (OSError, ValueError):
        return False
    return resolved == root.resolve() or root.resolve() in resolved.parents


def listeners() -> list[Listener]:
    """Every TCP socket in LISTEN state, on every address family."""
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        return []
    return [
        Listener(port=connection.laddr.port, address=connection.laddr.ip, pid=connection.pid)
        for connection in connections
        if connection.status == psutil.CONN_LISTEN and connection.laddr
    ]


def port_has_listener(port: int) -> bool:
    """True if any process listens on `port` on any address, IPv4 or IPv6."""
    return any(listener.port == port for listener in listeners())


def _wildcard_bind_succeeds(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
        return True


def port_is_free(port: int, reserved: Collection[int] = ()) -> bool:
    """Whether nothing listens on `port` and a wildcard bind succeeds."""
    return port not in reserved and not port_has_listener(port) and _wildcard_bind_succeeds(port)


def allocate_port(preferred: int | None, reserved: Collection[int] = ()) -> int:
    """Return a free port, trying `preferred` first.

    The probe closes before the service binds, so a race is possible; callers
    treat a bind failure at start as retryable and allocate again.

    Args:
        preferred: Port to try first so URLs stay stable across runs.
        reserved: Ports that must not be handed out (other software's, or
            already allocated to a sibling that has not bound yet).
    """
    if preferred is not None and port_is_free(preferred, reserved):
        return preferred
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("0.0.0.0", 0))
            candidate: int = probe.getsockname()[1]
        if candidate not in reserved and not port_has_listener(candidate):
            return candidate


def holders(ports: Iterable[int]) -> list[PortHolder]:
    """Describe every process listening on any of `ports`, one entry per (port, pid)."""
    wanted = set(ports)
    found: dict[tuple[int, int], PortHolder] = {}
    for listener in listeners():
        if listener.port not in wanted or listener.pid is None or (listener.port, listener.pid) in found:
            continue
        holder = describe(listener.pid, listener.port)
        if holder is not None:
            found[(listener.port, listener.pid)] = holder
    return list(found.values())


def describe(pid: int, port: int) -> PortHolder | None:
    """Snapshot a listening process, or None when it vanished or is inaccessible."""
    try:
        process = psutil.Process(pid)
        with process.oneshot():
            cwd: str | None
            try:
                cwd = process.cwd()
            except (psutil.AccessDenied, OSError):
                cwd = None
            try:
                cmdline = tuple(process.cmdline())
            except (psutil.AccessDenied, OSError):
                cmdline = ()
            return PortHolder(
                port=port, pid=pid, name=process.name(), cmdline=cmdline, cwd=cwd, create_time=process.create_time()
            )
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return PortHolder(port=port, pid=pid, name="?", cmdline=(), cwd=None, create_time=0.0)


def protected_pids() -> set[int]:
    """PIDs never to kill when reclaiming a port: system PIDs, us, and our ancestors."""
    protected = set(_SYSTEM_PIDS) | {os.getpid()}
    with contextlib.suppress(psutil.Error):
        protected |= {parent.pid for parent in psutil.Process().parents()}
    return protected


def reclaim(holder: PortHolder, timeout_s: float = 3.0) -> bool:
    """Terminate, then kill, the holder and its tree, verifying create_time against PID reuse.

    Returns:
        Whether the holder is gone.

    Raises:
        PermissionError: when the holder is a protected PID.
    """
    if holder.pid in protected_pids():
        raise PermissionError(f"refusing to kill protected process {holder.pid} ({holder.name})")
    try:
        process = psutil.Process(holder.pid)
        if process.create_time() != holder.create_time:
            return True  # the PID now names a different process; ours is already gone
        tree = [*process.children(recursive=True), process]
    except psutil.NoSuchProcess:
        return True
    for member in tree:
        with contextlib.suppress(psutil.NoSuchProcess):
            member.terminate()
    _gone, alive = psutil.wait_procs(tree, timeout=timeout_s)
    for member in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            member.kill()
    _gone, alive = psutil.wait_procs(alive, timeout=timeout_s)
    return not alive
