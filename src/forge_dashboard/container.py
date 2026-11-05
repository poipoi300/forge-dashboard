"""Construct every dashboard service with its dependencies, in start order."""

from __future__ import annotations

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.services.discovery import DiscoveryService
from forge_dashboard.services.gallery import GalleryService
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService


def build_runtime(paths: DataPaths) -> Runtime:
    """A runtime with every service registered (nothing started, no I/O)."""
    runtime = Runtime(paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git = GitService(runtime, projects, jobs)
    # Discovery starts last: every project listener must be registered before it publishes candidates.
    runtime.register(
        [
            jobs,
            scheduler,
            manifests,
            projects,
            git,
            GalleryService(runtime, jobs),
            discovery,
        ]
    )
    return runtime
