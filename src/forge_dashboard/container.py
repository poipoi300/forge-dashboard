"""Construct every dashboard service with its dependencies, in start order."""

from __future__ import annotations

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.services.agents import AgentsService
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import DiscoveryService
from forge_dashboard.services.forge_runs import ForgeRunsService
from forge_dashboard.services.forks import ForksService
from forge_dashboard.services.gallery import GalleryService
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.machine import MachineService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.metrics import MetricsService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService


def _no_supervised_services(_project_id: str, _service_id: str) -> str | None:
    """Until project services are supervised, no manifest service has a URL to read metrics from."""
    return None


def build_runtime(paths: DataPaths) -> Runtime:
    """A runtime with every service registered (nothing started, no I/O)."""
    runtime = Runtime(paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git, jobs, scheduler)
    metrics = MetricsService(runtime, projects, _no_supervised_services)
    agents = AgentsService(runtime, projects, jobs, checks)
    forks = ForksService(runtime, projects, git, jobs, scheduler, checks, agents)
    # Discovery starts last: every project listener must be registered before it publishes candidates.
    runtime.register(
        [
            jobs,
            scheduler,
            manifests,
            projects,
            git,
            checks,
            metrics,
            agents,
            forks,
            MachineService(runtime, projects),
            ForgeRunsService(runtime, projects),
            GalleryService(runtime, jobs),
            discovery,
        ]
    )
    return runtime
