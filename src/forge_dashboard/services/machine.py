"""The machine panel and the port map.

CPU, RAM, GPU, and the disks under the project roots refresh on a short
period; the listener table, which costs a process lookup per listener, on a
longer one. Blocking psutil calls run in worker threads, and `nvidia-smi`
goes through the process module like every other subprocess. A machine
without `nvidia-smi` simply has no GPUs.

The port map attributes each listener to the project whose root holds the
process's working directory or one of its command-line paths (the deepest
root wins) and flags ports that two processes listen on or that two projects'
manifests claim.
"""

from __future__ import annotations

import asyncio
import os
import platform
import socket
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, override

import psutil
from pydantic import BaseModel, Field

from forge_dashboard.core.service import NoInput, Service, action
from forge_dashboard.core.tasks import BackgroundTasks, repeat
from forge_dashboard.core.ui import ui
from forge_dashboard.manifest.models import DynamicPort, PinnedPort, PortPolicy
from forge_dashboard.process import ports, tree

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime
    from forge_dashboard.services.projects import ProjectRecord, ProjectsService

NVIDIA_SMI_ARGV = (
    "nvidia-smi",
    "--query-gpu=name,utilization.gpu,memory.used,memory.total",
    "--format=csv,noheader,nounits",
)
NVIDIA_SMI_TIMEOUT_S = 5.0
CPU_SAMPLE_S = 0.2
_MIB = 1024 * 1024

ConflictKind = Literal["multiple-listeners", "claimed-by-projects"]
CONFLICT_KIND_UI = ui(
    format="badge",
    enum_labels={"multiple-listeners": "several listeners", "claimed-by-projects": "claimed by projects"},
    tones={"multiple-listeners": "bad", "claimed-by-projects": "warn"},
)


class GpuState(BaseModel):
    name: str
    load_percent: float | None = Field(None, json_schema_extra=ui(format="percent", short_label="load"))
    memory_used: int | None = Field(None, json_schema_extra=ui(format="bytes", short_label="VRAM used"))
    memory_total: int | None = Field(None, json_schema_extra=ui(format="bytes", short_label="VRAM"))


class DiskState(BaseModel):
    mount: str = Field(json_schema_extra=ui(format="path", help="Drive or mount point holding project roots."))
    free: int | None = Field(None, json_schema_extra=ui(format="bytes", help="Unknown when the volume is unreachable."))
    total: int | None = Field(None, json_schema_extra=ui(format="bytes"))
    project_ids: list[str] = Field(default_factory=list, json_schema_extra=ui(short_label="projects"))


class PortEntry(BaseModel):
    port: int
    address: str
    pid: int | None = None
    process: str | None = Field(None, json_schema_extra=ui(help="Executable name of the listening process."))
    project_id: str | None = Field(
        None,
        json_schema_extra=ui(short_label="project", help="The project whose root holds the process's cwd or paths."),
    )


class PortConflict(BaseModel):
    port: int
    kind: ConflictKind = Field(json_schema_extra=CONFLICT_KIND_UI)
    pids: list[int] = Field(default_factory=list, json_schema_extra=ui(help="Processes listening on the port now."))
    project_ids: list[str] = Field(default_factory=list, json_schema_extra=ui(short_label="projects"))


class MachineConfig(BaseModel):
    interval_s: int = Field(
        5,
        ge=1,
        le=3600,
        json_schema_extra=ui(widget="duration", help="Refresh period for CPU, RAM, GPU, and disks."),
    )
    ports_interval_s: int = Field(
        10, ge=1, le=3600, json_schema_extra=ui(widget="duration", help="Refresh period for the port map.")
    )


class MachineState(BaseModel):
    hostname: str = Field(default_factory=socket.gethostname)
    operating_system: str = Field(default_factory=platform.platform, json_schema_extra=ui(short_label="OS"))
    cpu_count: int | None = Field(default_factory=psutil.cpu_count, json_schema_extra=ui(short_label="cores"))
    cpu_percent: float | None = Field(None, json_schema_extra=ui(format="percent", short_label="CPU"))
    ram_used: int | None = Field(None, json_schema_extra=ui(format="bytes", short_label="RAM used"))
    ram_total: int | None = Field(None, json_schema_extra=ui(format="bytes", short_label="RAM"))
    gpus: list[GpuState] = Field(
        default_factory=list,
        json_schema_extra=ui(columns=["name", "load_percent", "memory_used", "memory_total"]),
    )
    gpu_error: str | None = Field(None, json_schema_extra=ui(help="Why nvidia-smi is present but gave no GPUs."))
    disks: list[DiskState] = Field(
        default_factory=list, json_schema_extra=ui(columns=["mount", "free", "total", "project_ids"])
    )
    ports: list[PortEntry] = Field(
        default_factory=list, json_schema_extra=ui(columns=["port", "address", "process", "pid", "project_id"])
    )
    conflicts: list[PortConflict] = Field(
        default_factory=list, json_schema_extra=ui(columns=["port", "kind", "pids", "project_ids"])
    )
    hardware_sampled_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    ports_sampled_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))


# --- sampling (blocking; run in worker threads) ---


@dataclass(frozen=True)
class HardwareSample:
    cpu_percent: float
    ram_used: int
    ram_total: int


def sample_hardware() -> HardwareSample:
    """CPU load over a short window, and RAM in use."""
    memory = psutil.virtual_memory()
    return HardwareSample(
        cpu_percent=psutil.cpu_percent(interval=CPU_SAMPLE_S), ram_used=memory.used, ram_total=memory.total
    )


def mount_point(path: Path) -> Path:
    """The drive or mount point holding `path`, whether or not `path` still exists."""
    current = path.resolve()
    while not os.path.ismount(current) and current.parent != current:
        current = current.parent
    return current


def sample_disks(roots: Mapping[str, Path]) -> list[DiskState]:
    """Free and total space of each distinct mount holding a project root."""
    projects_by_mount: dict[Path, list[str]] = {}
    for project_id, root in roots.items():
        projects_by_mount.setdefault(mount_point(root), []).append(project_id)
    disks: list[DiskState] = []
    for mount, project_ids in projects_by_mount.items():
        try:
            usage = psutil.disk_usage(str(mount))
        except OSError:
            disks.append(DiskState(mount=str(mount), project_ids=sorted(project_ids)))
            continue
        disks.append(DiskState(mount=str(mount), free=usage.free, total=usage.total, project_ids=sorted(project_ids)))
    return sorted(disks, key=lambda disk: disk.mount)


def owning_project(holder: ports.PortHolder, roots: Mapping[str, Path]) -> str | None:
    """The project whose root contains the holder; the deepest root wins when roots nest.

    Only absolute command-line arguments count: `lives_inside` resolves the
    rest (`-k`, `RPCSS`) against the dashboard's own working directory, which
    would pin every such process on the dashboard's project.
    """
    located = replace(holder, cmdline=tuple(argument for argument in holder.cmdline if Path(argument).is_absolute()))
    containing = [(len(root.parts), project_id) for project_id, root in roots.items() if located.lives_inside(root)]
    return max(containing)[1] if containing else None


OwnerKey = tuple[int, float, str | None]


class ProjectOwners:
    """Which project owns each listening process, remembered between refreshes.

    Attribution resolves every path of a process against every project root,
    about a second of filesystem calls on a typical machine. A process that
    keeps its identity (pid, start time, cwd) under unchanged roots keeps its
    answer, and answers for processes that disappeared are dropped.
    """

    def __init__(self) -> None:
        self._roots: dict[str, Path] = {}
        self._known: dict[OwnerKey, str | None] = {}

    def resolve(self, holders: Iterable[ports.PortHolder], roots: Mapping[str, Path]) -> dict[int, str | None]:
        """Owning project id (or None) by pid."""
        known = self._known if dict(roots) == self._roots else {}
        remembered: dict[OwnerKey, str | None] = {}
        owners: dict[int, str | None] = {}
        for holder in holders:
            key = (holder.pid, holder.create_time, holder.cwd)
            remembered[key] = known[key] if key in known else owning_project(holder, roots)
            owners[holder.pid] = remembered[key]
        self._roots, self._known = dict(roots), remembered
        return owners


def sample_ports(roots: Mapping[str, Path], owners: ProjectOwners) -> list[PortEntry]:
    """Every TCP listener, with its process and owning project."""
    listeners = ports.listeners()
    holders: dict[int, ports.PortHolder] = {}
    for listener in listeners:
        if listener.pid is not None and listener.pid not in holders:
            holder = ports.describe(listener.pid, listener.port)
            if holder is not None:  # None: exited since the table was read
                holders[listener.pid] = holder
    owner_of = owners.resolve(holders.values(), roots)
    entries: list[PortEntry] = []
    for listener in listeners:
        if listener.pid is None:
            entries.append(PortEntry(port=listener.port, address=listener.address))
        elif listener.pid in holders:
            entries.append(
                PortEntry(
                    port=listener.port,
                    address=listener.address,
                    pid=listener.pid,
                    process=holders[listener.pid].name,
                    project_id=owner_of[listener.pid],
                )
            )
    return sorted(entries, key=lambda entry: (entry.port, entry.address, entry.pid or 0))


def _claimed_port(policy: PortPolicy | None) -> int | None:
    if isinstance(policy, PinnedPort):
        return policy.port
    if isinstance(policy, DynamicPort):
        return policy.preferred
    return None


def manifest_port_claims(records: Iterable[ProjectRecord]) -> dict[int, set[str]]:
    """Port to the projects whose manifests pin it or prefer it."""
    claims: dict[int, set[str]] = {}
    for record in records:
        if record.manifest is None:
            continue
        for service in record.manifest.services:
            claimed = _claimed_port(service.port)
            if claimed is not None:
                claims.setdefault(claimed, set()).add(record.id)
    return claims


def find_port_conflicts(entries: Iterable[PortEntry], claims: Mapping[int, Iterable[str]]) -> list[PortConflict]:
    """Ports with several listening processes, and ports two or more projects claim."""
    entries_by_port: dict[int, list[PortEntry]] = {}
    for entry in entries:
        entries_by_port.setdefault(entry.port, []).append(entry)
    conflicts: list[PortConflict] = []
    for port, listeners in entries_by_port.items():
        pids = sorted({entry.pid for entry in listeners if entry.pid is not None})
        if len(pids) > 1:
            owners = sorted({entry.project_id for entry in listeners if entry.project_id is not None})
            conflicts.append(PortConflict(port=port, kind="multiple-listeners", pids=pids, project_ids=owners))
    for port, claimants in claims.items():
        projects = sorted(set(claimants))
        if len(projects) > 1:
            pids = sorted({entry.pid for entry in entries_by_port.get(port, []) if entry.pid is not None})
            conflicts.append(PortConflict(port=port, kind="claimed-by-projects", pids=pids, project_ids=projects))
    return sorted(conflicts, key=lambda conflict: (conflict.port, conflict.kind))


# --- nvidia-smi ---


def _csv_number(text: str) -> float | None:
    """A number, or None for the placeholders nvidia-smi prints when it cannot measure (`[N/A]`)."""
    value = text.strip()
    if value.startswith("[") or value.upper() == "N/A":
        return None
    return float(value)


def parse_nvidia_smi(output: str) -> list[GpuState]:
    """GPUs from `--query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits`.

    Raises:
        ValueError: on a line that is not four comma-separated fields with numeric values.
    """
    gpus: list[GpuState] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        # Names may contain commas; the three numeric fields are always last.
        name, load, used, total = (part.strip() for part in line.rsplit(",", 3))
        used_mib, total_mib = _csv_number(used), _csv_number(total)
        gpus.append(
            GpuState(
                name=name,
                load_percent=_csv_number(load),
                memory_used=None if used_mib is None else int(used_mib * _MIB),
                memory_total=None if total_mib is None else int(total_mib * _MIB),
            )
        )
    return gpus


@dataclass(frozen=True)
class GpuQuery:
    gpus: list[GpuState]
    error: str | None = None


async def query_gpus() -> GpuQuery:
    """Ask `nvidia-smi`; no executable means no GPUs and no error, anything else wrong is reported."""
    try:
        completed = await tree.run(NVIDIA_SMI_ARGV, Path.cwd(), timeout_s=NVIDIA_SMI_TIMEOUT_S)
    except tree.ExecutableNotFoundError:
        return GpuQuery([])
    if completed.timed_out:
        return GpuQuery([], f"nvidia-smi gave no answer within {NVIDIA_SMI_TIMEOUT_S:g}s")
    if completed.exit_code != 0:
        detail = next((line for line in (completed.stderr + completed.stdout).splitlines() if line.strip()), "")
        return GpuQuery([], f"nvidia-smi exited with {completed.exit_code}" + (f": {detail}" if detail else ""))
    try:
        return GpuQuery(parse_nvidia_smi(completed.stdout))
    except ValueError as error:
        return GpuQuery([], f"unexpected nvidia-smi output: {error}")


# --- the service ---


class MachineService(Service[MachineConfig, MachineState]):
    id = "machine"
    title = "Machine"
    description = "Hostname, CPU, RAM, GPU, and project disks, plus a map of every listening TCP port."
    Config = MachineConfig
    State = MachineState

    def __init__(self, runtime: Runtime, projects: ProjectsService) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._owners = ProjectOwners()
        self._background = BackgroundTasks()

    @override
    async def start(self) -> None:
        self._background.spawn(
            repeat(self._refresh_hardware, lambda: self.config.interval_s, label="machine refresh"), "machine-hardware"
        )
        self._background.spawn(
            repeat(self._refresh_ports, lambda: self.config.ports_interval_s, label="machine refresh"), "machine-ports"
        )

    @override
    async def stop(self) -> None:
        await self._background.cancel_all()

    def _roots(self) -> dict[str, Path]:
        return {record.id: record.path for record in self._projects.records()}

    async def _refresh_hardware(self) -> None:
        roots = self._roots()
        hardware, disks, gpus = await asyncio.gather(
            asyncio.to_thread(sample_hardware), asyncio.to_thread(sample_disks, roots), query_gpus()
        )
        self.set_state(
            self.state.model_copy(
                update={
                    "cpu_percent": hardware.cpu_percent,
                    "ram_used": hardware.ram_used,
                    "ram_total": hardware.ram_total,
                    "gpus": gpus.gpus,
                    "gpu_error": gpus.error,
                    "disks": disks,
                    "hardware_sampled_at": time.time(),
                }
            )
        )

    async def _refresh_ports(self) -> None:
        roots = self._roots()
        entries = await asyncio.to_thread(sample_ports, roots, self._owners)
        conflicts = find_port_conflicts(entries, manifest_port_claims(self._projects.records()))
        self.set_state(
            self.state.model_copy(update={"ports": entries, "conflicts": conflicts, "ports_sampled_at": time.time()})
        )

    @action(title="Refresh")
    async def refresh(self, _request: NoInput) -> MachineState:
        """Sample the machine and the port map now."""
        await asyncio.gather(self._refresh_hardware(), self._refresh_ports())
        return self.state
