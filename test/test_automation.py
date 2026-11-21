"""Per-repository automation policies come only from dashboard settings."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import Git, write_files
from pydantic import ValidationError

from forge_dashboard.api.app import create_app
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.manifest import load_manifest
from forge_dashboard.services.automation import AutomationConfig, AutomationService, RepoAutomationPolicy
from forge_dashboard.services.projects import ProjectRecord

CONFIG_URL = "/api/services/automation/config"


@asynccontextmanager
async def running(paths: DataPaths) -> AsyncIterator[tuple[AutomationService, httpx.AsyncClient]]:
    runtime = Runtime(paths)
    service = AutomationService(runtime)
    runtime.register([service])
    await runtime.start()
    try:
        transport = httpx.ASGITransport(app=create_app(runtime))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            yield service, client
    finally:
        await runtime.stop()


def project_at(path: Path) -> ProjectRecord:
    load = load_manifest(path)
    return ProjectRecord(
        id=path.name,
        path=path,
        role="managed" if load.valid else "unmanifested",
        manifest=load.manifest,
        manifest_issues=tuple(load.issues),
        has_manifest_file=(path / "forge.project.toml").is_file(),
        is_git=(path / ".git").exists(),
    )


async def test_dashboard_loads_distinct_policies_for_repos_with_the_same_name(
    data_paths: DataPaths, tmp_path: Path, git: Git
) -> None:
    first = git.init(tmp_path / "local-alpha")
    second = git.init(tmp_path / "local-beta")
    git(first, "remote", "add", "origin", "git@github.com:TeamOne/Widget.git")
    git(second, "remote", "add", "origin", "https://github.com/TeamTwo/Widget")
    policies = {
        "repositories": [
            {
                "repo": "TeamOne/Widget",
                "owner": "Marie",
                "enabled": True,
                "automatic_actions": ["queue-issue"],
                "provider_route": "codex",
            },
            {
                "repo": "TeamTwo/Widget",
                "owner": "sam",
                "enabled": False,
                "automatic_actions": ["push"],
                "provider_route": "claude",
            },
        ]
    }

    async with running(data_paths) as (service, client):
        assert (await client.get(CONFIG_URL)).json() == {"repositories": []}
        assert await service.policy_for_project(project_at(first)) is None
        response = await client.put(CONFIG_URL, json=policies)
        assert response.status_code == 200
        stored = response.json()
        assert stored["repositories"][0]["repo"] == "teamone/widget"

    async with running(data_paths) as (service, client):
        assert (await client.get(CONFIG_URL)).json() == stored
        first_policy = await service.policy_for_project(project_at(first))
        assert first_policy is not None
        assert (first_policy.owner, first_policy.provider_route) == ("marie", "codex")
        assert first_policy.allows_automatic("queue-issue")
        assert not first_policy.allows_automatic("push")

        second_policy = await service.policy_for_project(project_at(second))
        assert second_policy is not None
        assert (second_policy.owner, second_policy.provider_route, second_policy.automatic_actions) == (
            "sam",
            "claude",
            ("push",),
        )
        assert not second_policy.allows_automatic("push")

        git(first, "remote", "set-url", "origin", "https://github.com/teamtwo/widget.git")
        assert await service.policy_for_project(project_at(first)) == second_policy
        git(first, "remote", "set-url", "origin", "https://git.example.com/teamone/widget.git")
        assert await service.policy_for_project(project_at(first)) is None
        git(first, "remote", "remove", "origin")
        assert await service.policy_for_project(project_at(first)) is None
        assert await service.policy_for_project(project_at(tmp_path)) is None


async def test_checkout_policy_requires_its_own_git_marker(data_paths: DataPaths, tmp_path: Path, git: Git) -> None:
    parent = git.init(tmp_path / "parent")
    git(parent, "remote", "add", "origin", "https://github.com/team/widget.git")
    write_files(parent, {"README.md": "Widget"})
    git.commit_all(parent, "Initial commit")
    checkout = parent / "linked"
    git(parent, "worktree", "add", "-b", "linked", str(checkout))
    record = project_at(checkout)
    async with running(data_paths) as (service, client):
        response = await client.put(
            CONFIG_URL,
            json={"repositories": [{"repo": "team/widget", "owner": "maintainer", "provider_route": "codex"}]},
        )
        assert response.status_code == 200
        assert await service.policy_for_project(record) == service.policy_for_repo("team/widget")
        (checkout / ".git").rename(checkout / ".old-git")
        assert await service.policy_for_project(record) is None


async def test_repository_policy_in_the_manifest_cannot_set_or_override_settings(
    data_paths: DataPaths, tmp_path: Path, git: Git
) -> None:
    checkout = git.init(tmp_path / "widget")
    git(checkout, "remote", "add", "origin", "https://github.com/team/widget.git")
    write_files(
        checkout,
        {
            "forge.project.toml": """
            schema = 1
            [project]
            id = "widget"
            name = "Widget"
            summary = "A widget"
            kind = "tool"
            languages = ["python"]
            [v1]
            source = "github-milestone"
            github_milestone = "V1"
            [automation]
            owner = "repo-author"
            enabled = true
            automatic_actions = ["push"]
            provider_route = "grok"
            """
        },
    )
    record = project_at(checkout)
    assert [issue.location for issue in record.manifest_issues] == ["automation"]
    async with running(data_paths) as (service, client):
        assert await service.policy_for_project(record) is None
        response = await client.put(
            CONFIG_URL,
            json={"repositories": [{"repo": "team/widget", "owner": "maintainer", "provider_route": "codex"}]},
        )
        assert response.status_code == 200
        policy = await service.policy_for_project(record)
        assert policy == RepoAutomationPolicy(repo="team/widget", owner="maintainer", provider_route="codex")
        assert not policy.enabled
        assert not policy.allows_automatic("push")


@pytest.mark.parametrize(
    "invalid",
    [
        {"repo": "https://github.com/team/widget"},
        {"repo": "team/widget/pulls"},
        {"owner": "@maintainer"},
        {"enabled": "true"},
        {"automatic_actions": ["everything"]},
        {"provider_route": "unknown"},
        {"provider_token": "irrelevant"},
    ],
)
def test_policy_rejects_invalid_identifiers_actions_and_routes(invalid: dict[str, Any]) -> None:
    policy = {"repo": "team/widget", "owner": "maintainer", "provider_route": "codex", **invalid}
    with pytest.raises(ValidationError):
        RepoAutomationPolicy.model_validate(policy)


async def test_settings_reject_duplicate_repos_and_cross_origin_writes(data_paths: DataPaths) -> None:
    row = {"repo": "Team/Widget", "owner": "maintainer", "provider_route": "grok"}
    async with running(data_paths) as (service, client):
        duplicate = await client.put(CONFIG_URL, json={"repositories": [row, {**row, "repo": " team/widget "}]})
        assert duplicate.status_code == 422
        assert "configure each GitHub repository only once" in duplicate.text

        forbidden = await client.put(
            CONFIG_URL, headers={"Origin": "https://example.com"}, json={"repositories": [row]}
        )
        assert forbidden.status_code == 403
        assert service.config == AutomationConfig()


async def test_failed_settings_write_cannot_enable_automation(
    data_paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(data_paths) as (service, _client):
        change = AutomationConfig(
            repositories=(
                RepoAutomationPolicy(
                    repo="team/widget",
                    owner="maintainer",
                    enabled=True,
                    provider_route="codex",
                    automatic_actions=("push",),
                ),
            )
        )

        async def fail_write(*_args: object) -> None:
            raise OSError("database unavailable")

        monkeypatch.setattr(service.runtime.db, "kv_put", fail_write)
        with pytest.raises(OSError, match="database unavailable"):
            await service.update_config(change)
        assert service.policy_for_repo("team/widget") is None
