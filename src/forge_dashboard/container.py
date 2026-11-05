"""Construct every dashboard service with its dependencies, in start order."""

from __future__ import annotations

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.github.bridge import GitHubBridge
from forge_dashboard.services.agents import AgentsService
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import DiscoveryService
from forge_dashboard.services.forge_runs import ForgeRunsService
from forge_dashboard.services.forks import ForksService
from forge_dashboard.services.gallery import GalleryService
from forge_dashboard.services.git import GitService
from forge_dashboard.services.github import GitHubService
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.machine import MachineService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.metrics import MetricsService
from forge_dashboard.services.project_services import ProjectServicesService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService
from forge_dashboard.services.standards import StandardsService


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
    project_services = ProjectServicesService(runtime, projects, jobs)
    metrics = MetricsService(runtime, projects, project_services.url_for)
    agents = AgentsService(runtime, projects, jobs, checks)
    forks = ForksService(runtime, projects, git, jobs, scheduler, checks, agents)
    github = GitHubService(runtime, projects, jobs)
    bridge = GitHubBridge(github)
    agents.connect_github(bridge, bridge)
    forks.connect_github(bridge.parent_clone_url)
    # Discovery starts last: every project listener must be registered before it publishes candidates.
    runtime.register(
        [
            jobs,
            scheduler,
            manifests,
            projects,
            git,
            github,
            checks,
            project_services,
            metrics,
            agents,
            forks,
            StandardsService(runtime, projects, jobs, checks, agents),
            MachineService(runtime, projects),
            ForgeRunsService(runtime, projects),
            GalleryService(runtime, jobs),
            discovery,
        ]
    )
    return runtime
