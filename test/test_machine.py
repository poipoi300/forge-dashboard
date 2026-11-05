"""The machine panel: hardware, disks, GPUs, the port map, and port conflicts."""

import asyncio
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import NamedTuple

import psutil
import pytest
from testing_support import wait_for

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import NoInput
from forge_dashboard.process import ports, tree
from forge_dashboard.process.ports import Listener, PortHolder
from forge_dashboard.services import machine as machine_module
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.machine import (
    GpuQuery,
    GpuState,
    MachineConfig,
    MachineService,
    MachineState,
    PortConflict,
    PortEntry,
    ProjectOwners,
    find_port_conflicts,
    manifest_port_claims,
    mount_point,
    owning_project,
    parse_nvidia_smi,
    query_gpus,
    sample_disks,
    sample_ports,
)
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService

MANIFEST = """
schema = 1

[project]
name = "{name}"
id = "{project_id}"
summary = "A fixture project"
kind = "app"
languages = ["python"]

[v1]
source = "milestones"

[[v1.milestones]]
id = "first"
title = "First milestone"
"""
SERVICE = """
[[services]]
id = "web"
label = "Web"
argv = ["serve", "--port", "{port}"]
port = %s
"""
PINNED_5173 = '{ policy = "pinned", port = 5173, evidence = "OAuth callback registered with the provider" }'
PREFERRING_5173 = '{ policy = "dynamic", preferred = 5173 }'
PORTLESS_MANIFEST_SERVICE = '\n[[services]]\nid = "worker"\nlabel = "Worker"\nargv = ["work"]\n'

LISTENER_SCRIPT = (
    "import socket, time\n"
    "server = socket.socket()\n"
    "server.bind(('127.0.0.1', 0))\n"
    "server.listen()\n"
    "print(server.getsockname()[1], flush=True)\n"
    "time.sleep(60)\n"
)


class Env(NamedTuple):
    machine: MachineService
    projects: ProjectsService
    discovery: DiscoveryService


@pytest.fixture
async def env(data_paths: DataPaths, tmp_path: Path, forge_repo: Path) -> AsyncIterator[Env]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    runtime.register([jobs, manifests, projects, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path)], "max_depth": 4})
    await runtime.start()
    await wait_for(lambda: projects.state.loaded)
    # Registered after start so the background loops stay off; tests sample on demand.
    machine = MachineService(runtime, projects)
    runtime.register([machine])
    try:
        yield Env(machine, projects, discovery)
    finally:
        await runtime.stop()


async def add(discovery: DiscoveryService, path: Path) -> None:
    await discovery.add_project(PathInput(path=str(path)))


# --- the snapshot ---


async def test_snapshot_reports_the_host_and_the_disks_under_project_roots(
    env: Env, make_project: Callable[..., Path]
) -> None:
    await add(env.discovery, make_project("Alpha"))
    snapshot = await env.machine.refresh(NoInput())
    assert snapshot is env.machine.state
    assert snapshot.hostname == socket.gethostname()
    assert snapshot.operating_system and (snapshot.cpu_count or 0) >= 1
    assert snapshot.cpu_percent is not None and 0 <= snapshot.cpu_percent <= 100
    assert snapshot.ram_used is not None and snapshot.ram_total is not None
    assert 0 < snapshot.ram_used <= snapshot.ram_total
    [disk] = snapshot.disks  # every fixture project lives under one tmp dir
    assert disk.free is not None and disk.total is not None and 0 < disk.free <= disk.total
    assert {"alpha", "forge"} <= set(disk.project_ids)
    assert snapshot.hardware_sampled_at is not None and snapshot.ports_sampled_at is not None


def test_a_volume_that_cannot_be_read_is_listed_without_sizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(_path: str) -> None:
        raise OSError("The network path was not found")

    monkeypatch.setattr(psutil, "disk_usage", unreachable)
    [disk] = sample_disks({"alpha": tmp_path})
    assert (disk.free, disk.total, disk.project_ids) == (None, None, ["alpha"])


def test_projects_on_the_same_volume_share_one_disk_entry(tmp_path: Path) -> None:
    disks = sample_disks({"b": tmp_path / "b", "a": tmp_path / "a"})
    assert [disk.project_ids for disk in disks] == [["a", "b"]]


def test_the_mount_of_a_missing_path_is_the_mount_of_its_nearest_existing_ancestor(tmp_path: Path) -> None:
    assert mount_point(tmp_path / "gone" / "deeper") == mount_point(tmp_path)
    anchor = Path(tmp_path.anchor)
    assert mount_point(anchor) == anchor


def test_state_schema_carries_the_display_hints_the_ui_renders_from() -> None:
    properties = MachineState.model_json_schema()["properties"]
    assert properties["ram_used"]["x-ui"]["format"] == "bytes"
    assert properties["ram_total"]["x-ui"]["format"] == "bytes"
    assert properties["cpu_percent"]["x-ui"]["format"] == "percent"
    for name in ("gpus", "disks", "ports", "conflicts"):
        assert properties[name]["x-ui"]["columns"]
    config = MachineConfig.model_json_schema()["properties"]
    assert config["interval_s"]["default"] == 5 and config["ports_interval_s"]["default"] == 10
    assert config["interval_s"]["x-ui"]["widget"] == "duration"


def test_refresh_is_the_only_action_and_it_is_not_a_query() -> None:
    [(action_id, meta, _input, output)] = MachineService.action_specs()
    assert (action_id, meta.query, meta.danger, output) == ("refresh", False, False, MachineState)


# --- the port map ---


async def test_a_listener_started_inside_a_project_is_attributed_to_it(
    env: Env, make_project: Callable[..., Path], tmp_path: Path
) -> None:
    alpha = make_project("Alpha")
    await add(env.discovery, alpha)
    inside, outside = alpha / "scripts", tmp_path / "elsewhere"
    inside.mkdir()
    outside.mkdir()
    owned_inside = await tree.spawn([sys.executable, "-c", LISTENER_SCRIPT], inside)
    owned_outside = await tree.spawn([sys.executable, "-c", LISTENER_SCRIPT], outside)
    try:
        inside_port = int(await asyncio.wait_for(anext(owned_inside.lines()), 30))
        outside_port = int(await asyncio.wait_for(anext(owned_outside.lines()), 30))
        snapshot = await env.machine.refresh(NoInput())
        [attributed] = [entry for entry in snapshot.ports if entry.port == inside_port]
        [stranger] = [entry for entry in snapshot.ports if entry.port == outside_port]
        inside_pids, outside_pids = owned_inside.tree_pids(), owned_outside.tree_pids()
    finally:
        await owned_inside.stop(grace_s=2)
        await owned_outside.stop(grace_s=2)
    assert attributed.address == "127.0.0.1" and attributed.project_id == "alpha"
    assert attributed.pid in inside_pids
    assert attributed.process is not None and attributed.process.lower().startswith("python")
    assert stranger.project_id is None and stranger.pid in outside_pids


def holder(cwd: Path | None, *cmdline: str, pid: int = 100, started: float = 1.0) -> PortHolder:
    return PortHolder(
        port=9000, pid=pid, name="app", cmdline=cmdline, cwd=str(cwd) if cwd else None, create_time=started
    )


def test_the_deepest_project_root_wins(tmp_path: Path) -> None:
    roots = {"outer": tmp_path / "outer", "inner": tmp_path / "outer" / "inner"}
    assert owning_project(holder(tmp_path / "outer" / "inner" / "src"), roots) == "inner"
    assert owning_project(holder(tmp_path / "outer" / "other"), roots) == "outer"
    assert owning_project(holder(tmp_path / "unrelated"), roots) is None


def test_an_absolute_command_line_path_attributes_a_process_whose_cwd_is_elsewhere(tmp_path: Path) -> None:
    roots = {"alpha": tmp_path / "alpha"}
    script = str(tmp_path / "alpha" / "server.py")
    assert owning_project(holder(tmp_path / "other", "python", script), roots) == "alpha"
    assert owning_project(holder(None, "python", script), roots) == "alpha"


def test_relative_arguments_never_resolve_against_the_dashboards_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "alpha"
    root.mkdir()
    monkeypatch.chdir(root)
    assert (
        owning_project(holder(tmp_path / "windows", r"C:\Windows\svchost.exe", "-k", "RPCSS"), {"alpha": root}) is None
    )


def test_attribution_is_remembered_until_the_process_or_the_roots_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def counting(candidate: PortHolder, roots: dict[str, Path]) -> str | None:
        calls.append(candidate.pid)
        return "alpha"

    monkeypatch.setattr(machine_module, "owning_project", counting)
    owners = ProjectOwners()
    roots = {"alpha": tmp_path}
    one, two = holder(tmp_path, pid=1), holder(tmp_path, pid=2)

    assert owners.resolve([one, two], roots) == {1: "alpha", 2: "alpha"}
    assert owners.resolve([one, two], roots) == {1: "alpha", 2: "alpha"}
    assert calls == [1, 2]
    owners.resolve([two], roots)  # pid 1 exits and is forgotten
    owners.resolve([one], roots)
    assert calls == [1, 2, 1]
    owners.resolve([one], {"alpha": tmp_path, "beta": tmp_path / "beta"})  # roots changed
    assert calls == [1, 2, 1, 1]
    restarted = holder(tmp_path, pid=1, started=2.0)
    owners.resolve([restarted], {"alpha": tmp_path, "beta": tmp_path / "beta"})
    assert calls == [1, 2, 1, 1, 1]


# --- conflicts ---


def entry(port: int, pid: int | None, project_id: str | None = None, address: str = "127.0.0.1") -> PortEntry:
    return PortEntry(port=port, address=address, pid=pid, process="app", project_id=project_id)


def test_two_processes_on_one_port_are_a_conflict() -> None:
    entries = [entry(5173, 20, "web"), entry(5173, 10, "api"), entry(5173, 10, "api", "::"), entry(8080, 30)]
    assert find_port_conflicts(entries, {}) == [
        PortConflict(port=5173, kind="multiple-listeners", pids=[10, 20], project_ids=["api", "web"])
    ]


def test_one_process_listening_on_both_ip_families_is_not_a_conflict() -> None:
    entries = [entry(8080, 10, address="0.0.0.0"), entry(8080, 10, address="::"), entry(9090, None), entry(9090, None)]
    assert find_port_conflicts(entries, {}) == []


def test_a_port_two_projects_claim_is_a_conflict_even_when_nothing_listens() -> None:
    claims = {5173: {"web", "api"}, 8000: {"web"}, 9000: ["solo", "solo"]}
    assert find_port_conflicts([], claims) == [
        PortConflict(port=5173, kind="claimed-by-projects", pids=[], project_ids=["api", "web"])
    ]


def test_a_claimed_port_names_the_processes_listening_on_it_and_conflicts_sort_by_port_then_kind() -> None:
    entries = [entry(5173, 1, "web"), entry(5173, 2, "api"), entry(3000, 5)]
    conflicts = find_port_conflicts(entries, {5173: {"web", "api"}, 3000: {"a", "b"}})
    assert [(conflict.port, conflict.kind) for conflict in conflicts] == [
        (3000, "claimed-by-projects"),
        (5173, "claimed-by-projects"),
        (5173, "multiple-listeners"),
    ]
    assert conflicts[0].pids == [5] and conflicts[1].pids == [1, 2]


async def test_manifests_that_pin_or_prefer_the_same_port_are_reported(
    env: Env, make_project: Callable[..., Path]
) -> None:
    claimants = {
        "Alpha": MANIFEST + SERVICE % PINNED_5173,
        "Beta": MANIFEST + SERVICE % PREFERRING_5173,
        "Gamma": MANIFEST + SERVICE % '{ policy = "dynamic" }',
        "Delta": MANIFEST + PORTLESS_MANIFEST_SERVICE,
    }
    for name, manifest in claimants.items():
        await add(env.discovery, make_project(name, manifest=manifest))
    await add(env.discovery, make_project("Plain", manifest=None))

    assert manifest_port_claims(env.projects.records()) == {5173: {"alpha", "beta"}}
    snapshot = await env.machine.refresh(NoInput())
    claimed = [conflict for conflict in snapshot.conflicts if conflict.kind == "claimed-by-projects"]
    assert [(conflict.port, conflict.project_ids) for conflict in claimed] == [(5173, ["alpha", "beta"])]


# --- nvidia-smi ---

NVIDIA_QUERY_ARGS = ["--query-gpu=name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]


def test_nvidia_smi_csv_becomes_gpus_in_bytes() -> None:
    output = "NVIDIA GeForce RTX 5090, 37, 2810, 32607\n\nNVIDIA RTX A4000, Laptop GPU, [N/A], [Not Supported], 8192\n"
    assert parse_nvidia_smi(output) == [
        GpuState(
            name="NVIDIA GeForce RTX 5090", load_percent=37.0, memory_used=2810 * 2**20, memory_total=32607 * 2**20
        ),
        GpuState(name="NVIDIA RTX A4000, Laptop GPU", load_percent=None, memory_used=None, memory_total=8192 * 2**20),
    ]
    assert parse_nvidia_smi("") == []


@pytest.mark.parametrize("output", ["no commas here", "gpu, high, 1, 2", "gpu, 5, 1"])
def test_unexpected_nvidia_smi_output_is_rejected(output: str) -> None:
    with pytest.raises(ValueError):
        parse_nvidia_smi(output)


def install_fake_nvidia_smi(directory: Path, monkeypatch: pytest.MonkeyPatch, python_body: str) -> None:
    """Put an `nvidia-smi` first on PATH that runs `python_body` with the real arguments."""
    directory.mkdir()
    script = directory / "fake_nvidia_smi.py"
    script.write_text(python_body, encoding="utf-8", newline="\n")
    if sys.platform == "win32":
        launcher = directory / "nvidia-smi.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8", newline="")
    else:
        launcher = directory / "nvidia-smi"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8", newline="\n")
        launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


async def test_a_machine_without_nvidia_smi_has_no_gpus_and_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(executable: str, env: object = None) -> str:
        raise tree.ExecutableNotFoundError(executable)

    monkeypatch.setattr(tree, "resolve_executable", missing)
    assert await query_gpus() == GpuQuery(gpus=[], error=None)


async def test_gpus_are_read_with_the_documented_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        "import sys\n"
        f"if sys.argv[1:] != {NVIDIA_QUERY_ARGS!r}:\n"
        "    sys.exit(2)\n"
        "print('NVIDIA GeForce RTX 5090, 12, 1024, 32607')\n"
    )
    install_fake_nvidia_smi(tmp_path / "bin", monkeypatch, body)
    assert await query_gpus() == GpuQuery(
        gpus=[
            GpuState(name="NVIDIA GeForce RTX 5090", load_percent=12.0, memory_used=2**30, memory_total=32607 * 2**20)
        ]
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "import sys\nprint('Driver not loaded', file=sys.stderr)\nsys.exit(9)\n",
            "nvidia-smi exited with 9: Driver not loaded",
        ),
        ("import sys\nsys.exit(3)\n", "nvidia-smi exited with 3"),
        ("print('garbled')\n", "unexpected nvidia-smi output"),
        ("import time\ntime.sleep(30)\n", "nvidia-smi gave no answer within 0.5s"),
    ],
)
async def test_a_failing_nvidia_smi_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, expected: str
) -> None:
    install_fake_nvidia_smi(tmp_path / "bin", monkeypatch, body)
    monkeypatch.setattr(machine_module, "NVIDIA_SMI_TIMEOUT_S", 0.5)
    result = await query_gpus()
    assert result.gpus == [] and result.error is not None and result.error.startswith(expected)


async def test_the_snapshot_includes_the_gpus_nvidia_smi_reports(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_nvidia_smi(tmp_path / "bin", monkeypatch, "print('Test GPU, 50, 100, 200')\n")
    snapshot = await env.machine.refresh(NoInput())
    assert snapshot.gpus == [
        GpuState(name="Test GPU", load_percent=50.0, memory_used=100 * 2**20, memory_total=200 * 2**20)
    ]
    assert snapshot.gpu_error is None


# --- the refresh loops ---


async def test_background_loops_sample_hardware_and_ports_until_stopped(env: Env) -> None:
    env.machine.config = MachineConfig(interval_s=1, ports_interval_s=1)
    await env.machine.start()
    try:
        await wait_for(lambda: env.machine.state.hardware_sampled_at is not None)
        await wait_for(lambda: env.machine.state.ports_sampled_at is not None)
    finally:
        await env.machine.stop()


async def test_a_failing_sample_is_logged_and_the_other_loop_keeps_going(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> None:
        raise RuntimeError("sensor offline")

    monkeypatch.setattr(machine_module, "sample_hardware", broken)
    env.machine.config = MachineConfig(interval_s=1, ports_interval_s=1)
    with caplog.at_level(logging.ERROR, logger="forge_dashboard"):
        await env.machine.start()
        try:
            await wait_for(lambda: env.machine.state.ports_sampled_at is not None)
            await wait_for(lambda: "machine refresh failed" in caplog.text)
        finally:
            await env.machine.stop()
    assert env.machine.state.hardware_sampled_at is None


def test_listeners_without_a_pid_are_kept_and_processes_that_exited_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = [
        Listener(port=445, address="0.0.0.0", pid=None),
        Listener(port=7000, address="127.0.0.1", pid=11),
        Listener(port=7001, address="127.0.0.1", pid=12),
    ]
    monkeypatch.setattr(ports, "listeners", lambda: table)
    monkeypatch.setattr(ports, "describe", lambda pid, port: holder(tmp_path, pid=pid) if pid == 11 else None)
    assert sample_ports({"alpha": tmp_path}, ProjectOwners()) == [
        PortEntry(port=445, address="0.0.0.0"),
        PortEntry(port=7000, address="127.0.0.1", pid=11, process="app", project_id="alpha"),
    ]
