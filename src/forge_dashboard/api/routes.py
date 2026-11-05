"""Non-service routes: the scripting API and report export."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from forge_dashboard.api.app import RouteInstaller
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.domain.report import render_markdown
from forge_dashboard.domain.summary import ProjectSummary
from forge_dashboard.services.projects import ProjectsService


def _projects(runtime: Runtime) -> ProjectsService:
    service = runtime.services["projects"]
    assert isinstance(service, ProjectsService)
    return service


def install_projects_api(router: APIRouter, runtime: Runtime) -> None:
    """`GET /api/projects` and `GET /api/projects/{id}`: the overview for scripts."""

    @router.get("/api/projects", operation_id="projects_list", tags=["scripting"])
    async def list_projects() -> list[ProjectSummary]:
        return _projects(runtime).state.projects

    @router.get("/api/projects/{project_id}", operation_id="projects_get", tags=["scripting"])
    async def get_project(project_id: str) -> ProjectSummary:
        for summary in _projects(runtime).state.projects:
            if summary.id == project_id:
                return summary
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")


def install_report(router: APIRouter, runtime: Runtime) -> None:
    """`GET /api/report.{md,json}`: the overview exported as a report."""

    @router.get("/api/report.{extension}", operation_id="report_export", tags=["scripting"], response_model=None)
    async def report(extension: Literal["md", "json"]) -> PlainTextResponse | list[ProjectSummary]:
        projects = _projects(runtime).state.projects
        if extension == "json":
            return projects
        headers = {"Content-Disposition": 'attachment; filename="forge-dashboard-report.md"'}
        return PlainTextResponse(render_markdown(projects), media_type="text/markdown", headers=headers)


ROUTE_INSTALLERS: tuple[RouteInstaller, ...] = (install_projects_api, install_report)
