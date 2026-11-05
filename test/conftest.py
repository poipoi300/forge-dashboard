"""Shared fixtures: an isolated data dir and a runtime with the core services."""

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.services.gallery import GalleryService
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.scheduler import SchedulerService


@pytest.fixture
def data_paths(tmp_path: Path) -> DataPaths:
    return DataPaths(root=tmp_path / "data")


def _core_runtime(paths: DataPaths) -> Runtime:
    runtime = Runtime(paths)
    jobs = JobsService(runtime)
    runtime.register([jobs, SchedulerService(runtime, jobs), GalleryService(runtime, jobs)])
    return runtime


@pytest.fixture
def make_runtime(data_paths: DataPaths) -> Callable[[], Runtime]:
    """Build a fresh runtime with jobs, scheduler, and gallery registered (not started) on the same data dir."""
    return lambda: _core_runtime(data_paths)


@pytest.fixture
async def started_runtime(data_paths: DataPaths) -> AsyncIterator[Runtime]:
    runtime = _core_runtime(data_paths)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No test ever touches the real per-user data dir."""
    monkeypatch.setenv("FORGE_DASHBOARD_DATA_DIR", str(tmp_path / "default-data"))
    yield
