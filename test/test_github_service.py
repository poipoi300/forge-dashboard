"""The GitHub service: remote detection, polling and facts, events, queries, writes, and token handling.

All GitHub traffic is mocked with respx; no test reaches the machine's real credential sources.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import keyring
import pytest
import respx
from conftest import Git
from fastapi.testclient import TestClient
from keyring.backends import fail
from pydantic import BaseModel, SecretStr, ValidationError
from test_github_auth import SECRET, FakeRunner, MemoryKeyring
from testing_support import wait_for

from forge_dashboard.api.app import create_app
from forge_dashboard.core.events import EventEnvelope
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, NoInput, ProjectRef
from forge_dashboard.domain.summary import CiFacts, IssueFacts
from forge_dashboard.github import auth as github_auth
from forge_dashboard.github.auth import AuthSource, GitHubAuth
from forge_dashboard.github.facts import (
    checks_from_check_runs,
    ci_facts,
    combine_checks,
    top_labels,
)
from forge_dashboard.github.models import (
    CloseIssueInput,
    CommentInput,
    CreateIssueInput,
    CreatePullRequestInput,
    IssueListInput,
    IssueRef,
    PullListInput,
    RunListInput,
    RunRef,
    SaveTokenInput,
    SetLabelsInput,
)
from forge_dashboard.github.payloads import (
    CheckRunPayload,
    CombinedStatusPayload,
    IssuePayload,
    RunPayload,
    WorkflowPayload,
)
from forge_dashboard.github.remote import parse_github_slug
from forge_dashboard.manifest import parse_manifest_text
from forge_dashboard.process import tree
from forge_dashboard.services import github as github_service
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.github import (
    CiFailure,
    CiRunCompleted,
    GitHubConfig,
    GitHubService,
    IssueOpened,
)
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectRecord, ProjectsService

SLUG = "octocat/Hello-World"
REPO_URL = "https://github.com/octocat/Hello-World"
REPO_PATH = f"/repos/{SLUG}"
SIGNED_IN = GitHubAuth(AuthSource.ENVIRONMENT, SECRET)
ANONYMOUS = GitHubAuth(AuthSource.NONE, None)
NOW = datetime.now(UTC).replace(microsecond=0)


# --- GitHub payload builders ---


def stamp(seconds_ago: float = 0.0) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rate_headers(remaining: int, limit: int = 60, reset_in_s: float = 3600.0) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(int(time.time() + reset_in_s)),
    }


def repo_json(**changes: object) -> dict[str, object]:
    return {
        "full_name": SLUG,
        "html_url": REPO_URL,
        "description": "My first repository",
        "default_branch": "main",
        "private": False,
        "fork": False,
        "archived": False,
        "open_issues_count": 4,
        **changes,
    }


def issue_json(
    number: int,
    title: str,
    *,
    labels: Sequence[str] = (),
    created_ago: float = 600,
    pull_request: bool = False,
    state: str = "open",
    assignees: Sequence[str] = (),
    milestone: str | None = None,
    comments: int = 0,
    body: str | None = "Details",
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "user": {"login": "monalisa"},
        "html_url": f"{REPO_URL}/issues/{number}",
        "labels": [{"name": label, "color": "ededed"} for label in labels],
        "assignees": [{"login": login} for login in assignees],
        "milestone": {"number": 3, "title": milestone, "state": "open", "open_issues": 1, "closed_issues": 2}
        if milestone
        else None,
        "comments": comments,
        "created_at": stamp(created_ago),
        "updated_at": stamp(created_ago / 2),
        **({"pull_request": {"url": f"{REPO_URL}/pulls/{number}"}} if pull_request else {}),
    }


def pull_json(number: int, sha: str, title: str = "A change") -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": "open",
        "draft": False,
        "user": {"login": "monalisa"},
        "html_url": f"{REPO_URL}/pull/{number}",
        "created_at": stamp(900),
        "updated_at": stamp(300),
        "head": {"ref": f"feature-{number}", "sha": sha},
        "base": {"ref": "main", "sha": "0" * 40},
    }


def workflow_json(workflow_id: int, name: str, state: str = "active") -> dict[str, object]:
    return {"id": workflow_id, "name": name, "path": f".github/workflows/{name.lower()}.yml", "state": state}


def run_json(
    run_id: int,
    workflow_id: int,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    ago: float = 300,
    duration_s: float = 90,
    branch: str = "main",
    attempt: int = 1,
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": f"run-name-{run_id}",
        "display_title": f"Commit message {run_id}",
        "workflow_id": workflow_id,
        "run_number": run_id - 90,
        "run_attempt": attempt,
        "event": "push",
        "head_branch": branch,
        "head_sha": "c" * 40,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"{REPO_URL}/actions/runs/{run_id}",
        "created_at": stamp(ago + duration_s),
        "run_started_at": stamp(ago + duration_s),
        "updated_at": stamp(ago),
    }


def passing_checks() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "success"}]},
        {"state": "success", "total_count": 1},
    )


def failing_checks() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "failure"}]},
        {"state": "success", "total_count": 1},
    )


SHA_A, SHA_B = "a" * 40, "b" * 40


def mock_repository(
    router: respx.MockRouter,
    *,
    headers: Mapping[str, str] | None = None,
    repo: dict[str, object] | None = None,
    issues: Sequence[dict[str, object]] | None = None,
    pulls: Sequence[dict[str, object]] | None = None,
    workflows: Sequence[dict[str, object]] | None = None,
    runs: Sequence[dict[str, object]] | None = None,
) -> dict[str, respx.Route]:
    """The REST traffic of one poll of octocat/Hello-World, with two issues, two pull requests, and two workflows."""
    answer = dict(headers or {})
    default_issues = [
        issue_json(7, "Crash on start", labels=["bug"], created_ago=600),
        issue_json(5, "Docs typo", labels=["bug", "docs"], created_ago=7200),
        issue_json(11, "Add feature", pull_request=True, created_ago=900),
    ]
    default_workflows = [workflow_json(1, "CI"), workflow_json(2, "Docs"), workflow_json(3, "Old", "disabled_manually")]
    default_runs = [
        run_json(101, 1, conclusion="failure", ago=300),
        run_json(100, 1, conclusion="success", ago=3600),
        run_json(102, 2, conclusion="success", ago=900),
        run_json(103, 3, conclusion="failure", ago=50),
    ]
    good_runs, good_status = passing_checks()
    bad_runs, bad_status = failing_checks()
    chosen_runs = list(runs if runs is not None else default_runs)
    return {
        "repo": router.get(REPO_PATH).respond(200, json=repo or repo_json(), headers=answer),
        "pulls": router.get(f"{REPO_PATH}/pulls").respond(
            200, json=list(pulls if pulls is not None else [pull_json(12, SHA_B), pull_json(11, SHA_A)]), headers=answer
        ),
        "issues": router.get(f"{REPO_PATH}/issues").respond(
            200, json=list(issues if issues is not None else default_issues), headers=answer
        ),
        "workflows": router.get(f"{REPO_PATH}/actions/workflows").respond(
            200,
            json={
                "total_count": 3,
                "workflows": list(workflows if workflows is not None else default_workflows),
            },
            headers=answer,
        ),
        "runs": router.get(f"{REPO_PATH}/actions/runs").respond(
            200, json={"total_count": len(chosen_runs), "workflow_runs": chosen_runs}, headers=answer
        ),
        "checks_a": router.get(f"{REPO_PATH}/commits/{SHA_A}/check-runs").respond(200, json=good_runs, headers=answer),
        "status_a": router.get(f"{REPO_PATH}/commits/{SHA_A}/status").respond(200, json=good_status, headers=answer),
        "checks_b": router.get(f"{REPO_PATH}/commits/{SHA_B}/check-runs").respond(200, json=bad_runs, headers=answer),
        "status_b": router.get(f"{REPO_PATH}/commits/{SHA_B}/status").respond(200, json=bad_status, headers=answer),
    }


# --- the harness ---


class AuthStub:
    """Replaces `resolve_auth` in the service: a fixed result, or the real chain over a fake runner."""

    def __init__(self) -> None:
        self.auth = ANONYMOUS
        self.calls = 0
        self.chain_runner: FakeRunner | None = None

    async def __call__(self, environ: Mapping[str, str] | None = None, run: Any = None) -> GitHubAuth:
        self.calls += 1
        if self.chain_runner is not None:
            return await github_auth.resolve_auth({}, self.chain_runner)
        return self.auth


@dataclass
class Harness:
    runtime: Runtime
    projects: ProjectsService
    discovery: DiscoveryService
    github: GitHubService
    auth: AuthStub
    facts: list[tuple[str, BaseModel]] = field(default_factory=list)
    events: asyncio.Queue[EventEnvelope] = field(default_factory=asyncio.Queue)

    def published(self, kind: type[BaseModel]) -> list[BaseModel]:
        return [facts for _project, facts in self.facts if isinstance(facts, kind)]

    def drain_events(self) -> list[EventEnvelope]:
        drained: list[EventEnvelope] = []
        while not self.events.empty():
            drained.append(self.events.get_nowait())
        return drained

    async def track(self, path: Path, project_id: str, manifest_text: str | None = None) -> ProjectRecord:
        manifest = parse_manifest_text(manifest_text).manifest if manifest_text is not None else None
        assert manifest_text is None or manifest is not None
        record = ProjectRecord(
            id=project_id,
            path=path,
            role="managed",
            manifest=manifest,
            manifest_issues=(),
            has_manifest_file=manifest is not None,
            is_git=(path / ".git").exists(),
        )
        await self.github.project_upserted(record)
        return record


@pytest.fixture(autouse=True)
def _isolated_credentials(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """No real environment token, keyring, or credential helper can leak into a test."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    real_run = tree.run

    async def guarded(
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        *,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
    ) -> tree.CompletedRun:
        if argv[0] == "gh" or list(argv[:2]) == ["git", "credential"]:
            raise AssertionError(f"a test reached the real credential source: {argv[0]}")
        return await real_run(argv, cwd, env, timeout_s=timeout_s, stdin=stdin)

    monkeypatch.setattr(tree, "run", guarded)
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory
    keyring.set_keyring(previous)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mocked:
        yield mocked


@pytest.fixture
async def harness(data_paths: DataPaths, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    projects = ProjectsService(runtime, discovery, ManifestsService(runtime))
    service = GitHubService(runtime, projects, jobs)
    runtime.register([jobs, projects, service, discovery])
    await runtime.open()
    stub = AuthStub()
    monkeypatch.setattr(github_service, "resolve_auth", stub)
    prepared = Harness(runtime, projects, discovery, service, stub)
    monkeypatch.setattr(projects, "put", lambda project_id, facts: prepared.facts.append((project_id, facts)))
    async with runtime.bus.subscribe() as events:
        prepared.events = events
        yield prepared
    await runtime.stop()


@pytest.fixture
async def hello(harness: Harness, make_project: Callable[..., Path], git: Git) -> ProjectRecord:
    """A project whose origin is github.com/octocat/Hello-World."""
    path = make_project("Hello", manifest=None)
    git(path, "remote", "add", "origin", f"{REPO_URL}.git")
    return await harness.track(path, "hello")


def request_of(route: respx.Route, index: int = -1) -> httpx.Request:
    request: httpx.Request = route.calls[index].request
    return request


def sent_json(route: respx.Route) -> Any:
    return json.loads(request_of(route).content)


# --- remote detection ---


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/octocat/Hello-World", SLUG),
        ("https://github.com/octocat/Hello-World.git", SLUG),
        ("https://github.com/octocat/Hello-World.git/", SLUG),
        ("http://github.com/octocat/Hello-World.git", SLUG),
        ("https://someone:ghp_embedded@github.com/octocat/Hello-World.git", SLUG),
        ("git@github.com:octocat/Hello-World.git", SLUG),
        ("git@github.com:octocat/Hello-World", SLUG),
        ("github.com:octocat/Hello-World.git", SLUG),
        ("ssh://git@github.com/octocat/Hello-World.git", SLUG),
        ("ssh://git@github.com:22/octocat/Hello-World.git", SLUG),
        ("ssh://git@ssh.github.com:443/octocat/Hello-World.git", SLUG),
        ("git://github.com/octocat/Hello-World.git", SLUG),
        ("HTTPS://GITHUB.COM/octocat/dotted.repo.name.git", "octocat/dotted.repo.name"),
        ("  https://github.com/octocat/Hello-World.git\n", SLUG),
        ("https://gitlab.com/octocat/Hello-World.git", None),
        ("git@gitlab.com:octocat/Hello-World.git", None),
        ("https://github.com.evil.example/octocat/Hello-World.git", None),
        ("https://github.com/octocat", None),
        ("https://github.com/octocat/Hello-World/extra", None),
        ("file:///srv/git/Hello-World.git", None),
        ("../sibling-repo", None),
        ("C:\\repos\\Hello-World", None),
        ("https://[bad", None),
        ("", None),
    ],
)
def test_github_slugs_come_from_every_remote_form(remote: str, expected: str | None) -> None:
    assert parse_github_slug(remote) == expected


# --- not on GitHub is a state, not an error ---


async def test_a_repo_without_a_github_remote_is_not_on_github(
    harness: Harness, make_project: Callable[..., Path], router: respx.MockRouter
) -> None:
    path = make_project("Local", manifest=None)
    await harness.track(path, "local")
    assert harness.published(IssueFacts) == [IssueFacts(github="not-on-github")]
    assert harness.published(CiFacts) == []
    state = harness.github.state.repos[0]
    assert (state.project_id, state.repo, state.link, state.fetched_at) == ("local", None, "not-on-github", None)
    await harness.github.refresh("local")
    assert await harness.github.poll_due() == [] and router.calls.call_count == 0


async def test_a_remote_elsewhere_and_a_non_git_directory_are_not_on_github(
    harness: Harness, make_project: Callable[..., Path], git: Git, tmp_path: Path
) -> None:
    elsewhere = make_project("Elsewhere", manifest=None)
    git(elsewhere, "remote", "add", "origin", "https://gitlab.com/octocat/Hello-World.git")
    await harness.track(elsewhere, "elsewhere")
    plain = tmp_path / "plain"
    plain.mkdir()
    await harness.track(plain, "plain")
    assert [link.link for link in harness.github.state.repos] == ["not-on-github", "not-on-github"]


async def test_adding_and_removing_the_remote_follows_git_changes(
    harness: Harness, make_project: Callable[..., Path], git: Git, router: respx.MockRouter
) -> None:
    mock_repository(router)
    path = make_project("Local", manifest=None)
    record = await harness.track(path, "local")
    git(path, "remote", "add", "origin", f"{REPO_URL}.git")
    await harness.github.files_changed(record, frozenset({"worktree"}))
    assert harness.github.state.repos[0].repo is None
    await harness.github.files_changed(record, frozenset({"git"}))
    assert harness.github.state.repos[0].repo == SLUG
    await harness.github.refresh("local")
    assert harness.github.repo_info("local") is not None
    git(path, "remote", "remove", "origin")
    await harness.github.files_changed(record, frozenset({"git"}))
    assert harness.github.state.repos[0].link == "not-on-github"
    assert harness.published(IssueFacts)[-1] == IssueFacts(github="not-on-github")
    assert harness.published(CiFacts)[-1] == CiFacts()
    assert harness.github.repo_info("local") is None


async def test_removed_projects_leave_the_state(harness: Harness, hello: ProjectRecord) -> None:
    await harness.github.project_removed("hello")
    assert harness.github.state.repos == []
    assert harness.github.repo_info("hello") is None


# --- facts ---


async def test_polling_publishes_issue_and_ci_facts_for_a_public_repo(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(router, headers=rate_headers(remaining=50))
    await harness.github.refresh("hello")
    assert harness.published(IssueFacts) == [
        IssueFacts(
            github="ok",
            repo=SLUG,
            open_issues=2,
            open_prs=2,
            top_labels=["bug", "docs"],
            newest_issue="Crash on start",
            html_url=REPO_URL,
        )
    ]
    (ci,) = harness.published(CiFacts)
    assert isinstance(ci, CiFacts)
    assert (ci.conclusion, ci.workflows, ci.failing_workflows) == ("failure", 2, ["CI"])
    assert ci.last_run_url == f"{REPO_URL}/actions/runs/101"
    assert ci.last_run_at == pytest.approx((NOW - timedelta(seconds=390)).timestamp(), abs=1)
    assert ci.open_pr_checks == "failing"
    assert all("authorization" not in call.request.headers for route in routes.values() for call in route.calls)
    assert request_of(routes["runs"]).url.params["branch"] == "main"
    assert request_of(routes["runs"]).url.params["exclude_pull_requests"] == "true"


async def test_state_reports_the_repo_the_fetch_time_and_the_budget(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(router, headers=rate_headers(remaining=42, limit=60, reset_in_s=1200))
    await harness.github.refresh("hello")
    state = harness.github.state
    assert (state.rate_limit, state.rate_remaining) == (60, 42)
    assert state.rate_reset_at == pytest.approx(time.time() + 1200, abs=5)
    assert (state.auth_source, state.authenticated) == (AuthSource.NONE, False)
    (repo,) = state.repos
    assert (repo.repo, repo.link, repo.detail) == (SLUG, "ok", None)
    assert repo.fetched_at == pytest.approx(time.time(), abs=5)
    info = harness.github.repo_info("hello")
    assert info is not None and (info.repo, info.default_branch, info.private, info.fork) == (
        SLUG,
        "main",
        False,
        False,
    )


async def test_a_listing_that_fills_its_pages_falls_back_to_the_repositorys_own_count(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(router, repo=repo_json(open_issues_count=1000), pulls=[pull_json(500, SHA_A)])
    pages = {
        "1": [issue_json(number, f"Issue {number}", labels=["bug"]) for number in range(1, 101)],
        "2": [issue_json(number, f"Issue {number}", labels=["bug"]) for number in range(101, 201)],
    }
    routes["issues"].mock(side_effect=lambda request: httpx.Response(200, json=pages[request.url.params["page"]]))
    await harness.github.refresh("hello")
    (issues,) = harness.published(IssueFacts)
    assert isinstance(issues, IssueFacts) and (issues.open_issues, issues.open_prs) == (999, 1)
    assert routes["issues"].call_count == 2


async def test_the_github_milestone_named_in_the_manifest_is_counted(
    harness: Harness, make_project: Callable[..., Path], git: Git, router: respx.MockRouter
) -> None:
    mock_repository(router)
    milestones = router.get(f"{REPO_PATH}/milestones").respond(
        200,
        json=[
            {"number": 2, "title": "V0", "state": "closed", "open_issues": 0, "closed_issues": 9},
            {"number": 3, "title": "V1", "state": "open", "open_issues": 4, "closed_issues": 6},
        ],
    )
    path = make_project("Hello", manifest=None)
    git(path, "remote", "add", "origin", f"{REPO_URL}.git")
    manifest = """
        schema = 1

        [project]
        name = "Hello"
        id = "hello"
        summary = "A fixture project"
        kind = "app"
        languages = ["python"]

        [v1]
        source = "github-milestone"
        github_milestone = "V1"
    """
    await harness.track(path, "hello", "\n".join(line.removeprefix("        ") for line in manifest.splitlines()))
    await harness.github.refresh("hello")
    (issues,) = harness.published(IssueFacts)
    assert isinstance(issues, IssueFacts) and (issues.milestone_open, issues.milestone_closed) == (4, 6)
    assert request_of(milestones).url.params["state"] == "all"
    assert harness.github.state.repos[0].detail is None


async def test_a_missing_github_milestone_is_reported_without_failing_the_link(
    harness: Harness, make_project: Callable[..., Path], git: Git, router: respx.MockRouter
) -> None:
    mock_repository(router)
    router.get(f"{REPO_PATH}/milestones").respond(200, json=[])
    path = make_project("Hello", manifest=None)
    git(path, "remote", "add", "origin", f"{REPO_URL}.git")
    await harness.track(
        path,
        "hello",
        'schema = 1\n[project]\nname = "Hello"\nid = "hello"\nsummary = "s"\nkind = "app"\nlanguages = ["python"]\n'
        '[v1]\nsource = "github-milestone"\ngithub_milestone = "V9"\n',
    )
    await harness.github.refresh("hello")
    (issues,) = harness.published(IssueFacts)
    assert isinstance(issues, IssueFacts) and issues.milestone_open is None
    assert harness.github.state.repos[0].link == "ok"
    assert harness.github.state.repos[0].detail == "GitHub milestone 'V9' not found"


async def test_a_repo_with_no_workflows_has_no_ci(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(router, workflows=[], runs=[], pulls=[], issues=[])
    await harness.github.refresh("hello")
    (ci,) = harness.published(CiFacts)
    assert ci == CiFacts()


async def test_a_workflow_quiet_for_the_last_hundred_runs_is_asked_for_individually(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(
        router, workflows=[workflow_json(1, "CI"), workflow_json(2, "Nightly")], runs=[run_json(101, 1)]
    )
    routes["runs"].respond(200, json={"total_count": 230, "workflow_runs": [run_json(101, 1)]})
    own = router.get(f"{REPO_PATH}/actions/workflows/2/runs").respond(
        200, json={"total_count": 1, "workflow_runs": [run_json(77, 2, conclusion="failure", ago=86400 * 3)]}
    )
    await harness.github.refresh("hello")
    (ci,) = harness.published(CiFacts)
    assert isinstance(ci, CiFacts) and (ci.conclusion, ci.failing_workflows) == ("failure", ["Nightly"])
    assert request_of(own).url.params["per_page"] == "1" and request_of(own).url.params["branch"] == "main"


async def test_pull_checks_are_fetched_only_up_to_the_configured_limit(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(router)
    await harness.github.update_config(GitHubConfig(pull_check_limit=1))
    await harness.github.refresh("hello")
    assert routes["checks_b"].call_count == 1 and routes["checks_a"].call_count == 0
    (ci,) = harness.published(CiFacts)
    assert isinstance(ci, CiFacts) and ci.open_pr_checks == "failing"


async def test_authenticated_polling_reads_pull_checks_with_one_graphql_query(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    routes = mock_repository(router)

    def node(number: int, rollup: str | None) -> dict[str, object]:
        commits = [{"commit": {"statusCheckRollup": {"state": rollup} if rollup else None}}]
        return {
            "number": number,
            "title": f"PR {number}",
            "isDraft": False,
            "url": f"{REPO_URL}/pull/{number}",
            "createdAt": stamp(900),
            "updatedAt": stamp(300),
            "headRefName": f"feature-{number}",
            "headRefOid": "d" * 40,
            "baseRefName": "main",
            "author": {"login": "monalisa"},
            "commits": {"nodes": commits},
        }

    graphql = router.post("/graphql").respond(
        200,
        json={
            "data": {"repository": {"pullRequests": {"totalCount": 7, "nodes": [node(1, "PENDING"), node(2, None)]}}}
        },
    )
    await harness.github.refresh("hello")
    variables = sent_json(graphql)["variables"]
    assert variables == {"owner": "octocat", "name": "Hello-World", "states": ["OPEN"], "first": 30}
    assert request_of(graphql).headers["authorization"] == f"Bearer {SECRET}"
    assert routes["pulls"].call_count == 0 and routes["checks_a"].call_count == 0
    issues, ci = harness.published(IssueFacts)[0], harness.published(CiFacts)[0]
    assert isinstance(issues, IssueFacts) and issues.open_prs == 7
    assert isinstance(ci, CiFacts) and ci.open_pr_checks == "pending"


# --- failures become link states ---


async def test_a_private_repo_without_a_token_asks_to_sign_in(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(REPO_PATH).respond(404, json={"message": "Not Found"})
    await harness.github.refresh("hello")
    assert harness.published(IssueFacts) == [IssueFacts(github="unauthenticated", repo=SLUG)]
    repo = harness.github.state.repos[0]
    assert repo.link == "unauthenticated" and repo.detail is not None and "sign in" in repo.detail


async def test_a_repo_a_token_cannot_see_is_an_error_not_a_sign_in(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    router.get(REPO_PATH).respond(404, json={"message": "Not Found"})
    await harness.github.refresh("hello")
    assert harness.published(IssueFacts) == [IssueFacts(github="error", repo=SLUG)]
    assert harness.github.state.repos[0].detail == "not found, or the token cannot read it"


async def test_a_rejected_token_reruns_the_auth_chain_and_names_its_source(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    router.get(REPO_PATH).respond(401, json={"message": "Bad credentials"})
    await harness.github.refresh("hello")
    calls_after_first_poll = harness.auth.calls
    repo = harness.github.state.repos[0]
    assert repo.link == "unauthenticated" and repo.detail == "GitHub rejected the token from GH_TOKEN or GITHUB_TOKEN"
    assert calls_after_first_poll == 2  # once to build the client, once to pick up a rotated credential


async def test_rate_limited_polls_keep_the_last_numbers_and_wait_for_the_reset(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(router, headers=rate_headers(remaining=50))
    await harness.github.refresh("hello")
    assert (routes["repo"].call_count, routes["pulls"].call_count) == (1, 1)
    mock_repository(router, headers=rate_headers(remaining=3, limit=60, reset_in_s=1800))
    await harness.github.refresh("hello")  # the first answer shows the budget is under the reserve
    assert (routes["repo"].call_count, routes["pulls"].call_count) == (2, 1)
    requests_so_far = router.calls.call_count
    await harness.github.refresh("hello")
    assert router.calls.call_count == requests_so_far  # the gate refused before any request
    last = harness.published(IssueFacts)[-1]
    assert isinstance(last, IssueFacts)
    assert (last.github, last.open_issues, last.open_prs) == ("rate-limited", 2, 2)
    assert harness.github.state.repos[0].link == "rate-limited"
    assert harness.github.state.rate_remaining == 3
    assert harness.github._tracked["hello"].due_at - time.monotonic() > 1500


async def test_github_refusing_with_a_rate_limit_is_reported_the_same_way(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(REPO_PATH).respond(
        403, json={"message": "API rate limit exceeded"}, headers=rate_headers(remaining=0, reset_in_s=600)
    )
    await harness.github.refresh("hello")
    assert harness.published(IssueFacts) == [IssueFacts(github="rate-limited", repo=SLUG)]
    assert harness.github._tracked["hello"].due_at - time.monotonic() > 500


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={"full_name": SLUG}),
        httpx.ConnectError("unreachable"),
    ],
    ids=["server-error", "unexpected-shape", "unreachable"],
)
async def test_other_failures_are_errors_with_a_reason(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter, failure: httpx.Response | Exception
) -> None:
    router.get(REPO_PATH).mock(side_effect=[failure])
    await harness.github.refresh("hello")
    repo = harness.github.state.repos[0]
    assert repo.link == "error" and repo.detail
    assert harness.published(IssueFacts) == [IssueFacts(github="error", repo=SLUG)]


# --- polling schedule ---


async def test_idle_repos_are_polled_slowly_and_busy_ones_quickly(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(router)
    await harness.github.refresh("hello")
    assert 280 < harness.github._tracked["hello"].due_at - time.monotonic() <= 300
    mock_repository(router, runs=[run_json(104, 1, status="in_progress", conclusion=None, ago=10)])
    await harness.github.refresh("hello")
    assert 20 < harness.github._tracked["hello"].due_at - time.monotonic() <= 30
    (ci, *_rest) = reversed(harness.published(CiFacts))
    assert isinstance(ci, CiFacts) and ci.conclusion == "in_progress"


async def test_poll_due_refreshes_only_what_is_due_and_isolates_failures(
    harness: Harness,
    hello: ProjectRecord,
    make_project: Callable[..., Path],
    git: Git,
    router: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_repository(router)
    second = make_project("Second", manifest=None)
    git(second, "remote", "add", "origin", "https://github.com/octocat/Spoon-Knife.git")
    await harness.track(second, "second")
    local = make_project("Local", manifest=None)
    await harness.track(local, "local")
    original = harness.github.refresh

    async def flaky(project_id: str) -> None:
        if project_id == "second":
            raise RuntimeError("bug in one poll")
        await original(project_id)

    monkeypatch.setattr(harness.github, "refresh", flaky)
    with caplog.at_level(logging.ERROR, logger="forge_dashboard"):
        assert sorted(await harness.github.poll_due()) == ["hello", "second"]
    assert "GitHub poll of second failed" in caplog.text
    assert await harness.github.poll_due() == ["second"]  # hello is no longer due; second never recorded a poll


async def test_the_poll_loop_polls_new_projects_at_once_and_wakes_on_refresh(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(router)
    await harness.github.start()
    try:
        await wait_for(lambda: harness.published(IssueFacts) != [])
        assert routes["repo"].call_count == 1
        await harness.github.refresh_now(ProjectRef(project_id="hello"))
        await wait_for(lambda: routes["repo"].call_count == 2)
    finally:
        await harness.github.stop()


async def test_requests_work_after_the_service_is_stopped_and_started_again(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(router)
    await harness.github.start()
    try:
        await wait_for(lambda: harness.published(IssueFacts) != [])
    finally:
        await harness.github.stop()
    polls_before = routes["repo"].call_count

    await harness.github.start()  # what a restore does after replacing the database
    try:
        info = await harness.github.repo(ProjectRef(project_id="hello"))
        assert info.repo == SLUG
        assert routes["repo"].call_count == polls_before + 1
        await harness.github.refresh_now(ProjectRef(project_id="hello"))
        await wait_for(lambda: routes["repo"].call_count == polls_before + 2)  # the action, then the poll it woke
    finally:
        await harness.github.stop()
    assert harness.auth.calls == 2  # each run built its own client and resolved the credential for it


async def test_the_poll_loop_survives_a_round_that_raises(
    harness: Harness,
    hello: ProjectRecord,
    router: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_repository(router)
    original = harness.github.poll_due
    rounds: list[int] = []

    async def flaky() -> list[str]:
        rounds.append(len(rounds))
        if len(rounds) == 1:
            raise RuntimeError("the round broke")
        return await original()

    monkeypatch.setattr(harness.github, "poll_due", flaky)
    await harness.github.start()
    try:
        await wait_for(lambda: rounds != [])
        await harness.github.refresh_now(ProjectRef(project_id="hello"))  # wakes the loop for its next round
        await wait_for(lambda: harness.published(IssueFacts) != [])
    finally:
        await harness.github.stop()
    assert "GitHub poll failed" in caplog.text


async def test_changing_the_config_applies_the_reserve_and_polls_again(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(router)
    await harness.github.refresh("hello")
    client = await harness.github._api()
    assert client.reserve_fraction == pytest.approx(0.1)
    await harness.github.update_config(GitHubConfig(rate_reserve_percent=25))
    assert client.reserve_fraction == pytest.approx(0.25)
    assert harness.github._tracked["hello"].due_at == 0.0


async def test_the_whole_path_runs_through_the_project_registry(
    data_paths: DataPaths,
    make_project: Callable[..., Path],
    git: Git,
    forge_repo: Path,
    tmp_path: Path,
    router: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    projects = ProjectsService(runtime, discovery, ManifestsService(runtime))
    service = GitHubService(runtime, projects, jobs)
    runtime.register([jobs, projects, service, discovery])
    stub = AuthStub()
    monkeypatch.setattr(github_service, "resolve_auth", stub)
    mock_repository(router)
    path = make_project("Hello")
    git(path, "remote", "add", "origin", f"{REPO_URL}.git")
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path)], "max_depth": 4})
    await runtime.start()
    try:
        await wait_for(lambda: projects.state.loaded)
        await discovery.add_project(PathInput(path=str(path)))
        await wait_for(
            lambda: projects.summary("hello").issues is not None and projects.summary("hello").ci is not None
        )
        issues, ci = projects.summary("hello").issues, projects.summary("hello").ci
        assert issues is not None and (issues.github, issues.open_issues, issues.open_prs) == ("ok", 2, 2)
        assert ci is not None and ci.conclusion == "failure"
        assert projects.summary("forge").issues == IssueFacts(github="not-on-github")
        assert {repo.project_id: repo.link for repo in service.state.repos}["forge"] == "not-on-github"
    finally:
        await runtime.stop()


# --- events ---


async def test_a_failed_default_branch_run_is_announced_once_with_a_dedupe_key(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(router)
    await harness.github.refresh("hello")
    announced = {envelope.type: envelope for envelope in harness.drain_events()}
    failure, completed_run = announced["github.ci-failure"], announced["github.ci-run-completed"]
    assert failure.project_id == "hello" and failure.persist and failure.notify_kind == "ci-failure"
    assert failure.severity == "error" and failure.title == "CI failed on main: CI #11"
    assert failure.dedupe_key == f"github-run:{SLUG}:101:1"
    assert failure.ts == pytest.approx((NOW - timedelta(seconds=300)).timestamp(), abs=1)
    assert failure.data["html_url"] == f"{REPO_URL}/actions/runs/101" and failure.data["conclusion"] == "failure"
    assert completed_run.persist and completed_run.notify_kind is None and completed_run.severity == "success"
    assert completed_run.title == "CI success on main: Docs #12"
    assert completed_run.dedupe_key == f"github-run:{SLUG}:102:1"
    await harness.github.refresh("hello")
    assert [e.type for e in harness.drain_events()] == []


async def test_a_rerun_of_the_same_run_is_announced_again(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(router, workflows=[workflow_json(1, "CI")], runs=[run_json(101, 1, conclusion="failure")])
    await harness.github.refresh("hello")
    mock_repository(
        router, workflows=[workflow_json(1, "CI")], runs=[run_json(101, 1, conclusion="failure", attempt=2)]
    )
    await harness.github.refresh("hello")
    keys = [e.dedupe_key for e in harness.drain_events() if e.type == "github.ci-failure"]
    assert keys == [f"github-run:{SLUG}:101:1", f"github-run:{SLUG}:101:2"]


async def test_runs_in_progress_and_runs_older_than_a_day_are_not_announced(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(
        router,
        workflows=[workflow_json(1, "CI"), workflow_json(2, "Docs")],
        runs=[
            run_json(101, 1, status="in_progress", conclusion=None, ago=10),
            run_json(102, 2, conclusion="failure", ago=3 * 86400),
        ],
        issues=[issue_json(9, "Ancient", created_ago=5 * 86400)],
    )
    await harness.github.refresh("hello")
    assert harness.drain_events() == []
    (ci,) = harness.published(CiFacts)
    assert isinstance(ci, CiFacts) and ci.conclusion == "failure"  # still counted in health, just not news


async def test_new_issues_are_announced_once_and_pull_requests_are_not(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(router, runs=[])
    await harness.github.refresh("hello")
    opened = [e for e in harness.drain_events() if e.type == "github.issue-opened"]
    assert sorted(e.dedupe_key or "" for e in opened) == [f"github-issue:{SLUG}:5", f"github-issue:{SLUG}:7"]
    crash = next(e for e in opened if e.data["number"] == 7)
    assert crash.persist and crash.notify_kind is None and crash.severity == "info"
    assert crash.title == "Issue #7 opened: Crash on start"
    assert crash.ts == pytest.approx((NOW - timedelta(seconds=600)).timestamp(), abs=1)
    assert crash.data["author"] == "monalisa" and crash.data["html_url"] == f"{REPO_URL}/issues/7"
    mock_repository(router, runs=[], issues=[issue_json(8, "Another"), *[issue_json(7, "Crash on start")]])
    await harness.github.refresh("hello")
    assert [e.dedupe_key for e in harness.drain_events()] == [f"github-issue:{SLUG}:8"]


def test_events_are_declared_with_unique_types() -> None:
    types = [event.type for event in GitHubService.events]
    assert types == ["github.ci-failure", "github.ci-run-completed", "github.issue-opened"]
    assert CiFailure.notify_kind == "ci-failure" and CiRunCompleted.notify_kind is None
    assert IssueOpened.persist and CiFailure.persist and CiRunCompleted.persist


# --- queries ---


async def test_issue_queries_filter_server_side_and_drop_pull_requests(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    milestones = router.get(f"{REPO_PATH}/milestones").respond(
        200, json=[{"number": 3, "title": "V1", "state": "open", "open_issues": 1, "closed_issues": 2}]
    )
    listing = router.get(f"{REPO_PATH}/issues").respond(
        200,
        json=[
            issue_json(7, "Crash", labels=["bug", "help wanted"], assignees=["octocat"], milestone="V1", comments=2),
            issue_json(11, "A pull request", pull_request=True),
            issue_json(5, "Typo", state="closed"),
        ],
    )
    result = await harness.github.issues(
        IssueListInput(
            project_id="hello", state="all", labels=["bug", "help wanted"], assignee="octocat", milestone="V1"
        )
    )
    params = request_of(listing).url.params
    assert (params["state"], params["labels"], params["assignee"], params["milestone"]) == (
        "all",
        "bug,help wanted",
        "octocat",
        "3",
    )
    assert request_of(milestones).url.params["state"] == "all"
    assert [issue.number for issue in result.issues] == [7, 5] and not result.truncated
    crash = result.issues[0]
    assert (crash.labels, crash.assignees, crash.milestone, crash.comments, crash.author) == (
        ["bug", "help wanted"],
        ["octocat"],
        "V1",
        2,
        "monalisa",
    )
    assert result.issues[1].state == "closed"


async def test_issue_queries_report_truncation_and_unknown_milestones(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(f"{REPO_PATH}/issues").respond(200, json=[issue_json(n, f"Issue {n}") for n in range(1, 6)])
    router.get(f"{REPO_PATH}/milestones").respond(200, json=[])
    result = await harness.github.issues(IssueListInput(project_id="hello", limit=3))
    assert [issue.number for issue in result.issues] == [1, 2, 3] and result.truncated
    with pytest.raises(ActionError, match="no milestone titled 'V9'") as missing:
        await harness.github.issues(IssueListInput(project_id="hello", milestone="V9"))
    assert missing.value.status == 404


async def test_issue_detail_carries_the_markdown_body_and_comments(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(f"{REPO_PATH}/issues/7").respond(
        200, json=issue_json(7, "Crash", body="## Steps\n\n1. Start it", comments=2)
    )
    comments = router.get(f"{REPO_PATH}/issues/7/comments").respond(
        200,
        json=[
            {
                "id": 1,
                "user": {"login": "octocat"},
                "body": "Reproduced",
                "html_url": f"{REPO_URL}/issues/7#issuecomment-1",
                "created_at": stamp(60),
            },
            {
                "id": 2,
                "user": None,
                "body": None,
                "html_url": f"{REPO_URL}/issues/7#issuecomment-2",
                "created_at": stamp(30),
            },
        ],
    )
    detail = await harness.github.issue(IssueRef(project_id="hello", number=7))
    assert detail.body == "## Steps\n\n1. Start it" and detail.issue.title == "Crash"
    assert [(c.author, c.body) for c in detail.comments] == [("octocat", "Reproduced"), (None, "")]
    assert not detail.comments_truncated and request_of(comments).url.params["per_page"] == "100"


async def test_pull_requests_are_listed_with_their_checks(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(router)
    listing = await harness.github.pulls(PullListInput(project_id="hello", limit=5))
    assert [(pull.number, pull.checks, pull.head_branch) for pull in listing.pulls] == [
        (12, "failing", "feature-12"),
        (11, "passing", "feature-11"),
    ]
    assert listing.pulls[0].author == "monalisa" and listing.pulls[0].base_branch == "main"
    assert request_of(routes["pulls"]).url.params["state"] == "open"


async def test_authenticated_pull_queries_use_graphql_states(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    graphql = router.post("/graphql").respond(
        200, json={"data": {"repository": {"pullRequests": {"totalCount": 0, "nodes": []}}}}
    )
    listing = await harness.github.pulls(PullListInput(project_id="hello", state="closed", limit=10))
    assert listing.pulls == [] and sent_json(graphql)["variables"]["states"] == ["CLOSED", "MERGED"]


async def test_workflow_runs_can_be_filtered_by_branch(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    routes = mock_repository(router)
    routes["runs"].respond(
        200,
        json={
            "total_count": 2,
            "workflow_runs": [
                run_json(101, 1, conclusion="failure", branch="feature", duration_s=125),
                run_json(110, 2, status="in_progress", conclusion=None, branch="feature"),
            ],
        },
    )
    listing = await harness.github.runs(RunListInput(project_id="hello", branch="feature", limit=5))
    params = request_of(routes["runs"]).url.params
    assert (params["branch"], params["per_page"]) == ("feature", "5")
    assert [(run.workflow, run.outcome, run.branch) for run in listing.runs] == [
        ("CI", "failure", "feature"),
        ("Docs", "in_progress", "feature"),
    ]
    assert listing.runs[0].duration_s == 125 and listing.runs[1].duration_s is None
    assert [workflow.name for workflow in listing.workflows] == ["CI", "Docs", "Old"]
    await harness.github.runs(RunListInput(project_id="hello"))
    assert "branch" not in request_of(routes["runs"]).url.params


async def test_run_details_list_jobs_steps_and_log_pages(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(f"{REPO_PATH}/actions/runs/101").respond(200, json=run_json(101, 1, conclusion="failure"))
    router.get(f"{REPO_PATH}/actions/workflows/1").respond(200, json=workflow_json(1, "CI"))
    router.get(f"{REPO_PATH}/actions/runs/101/jobs").respond(
        200,
        json={
            "total_count": 1,
            "jobs": [
                {
                    "id": 900,
                    "name": "test",
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": stamp(200),
                    "completed_at": stamp(140),
                    "html_url": f"{REPO_URL}/actions/runs/101/job/900",
                    "steps": [
                        {"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success"},
                        {"number": 2, "name": "pytest", "status": "completed", "conclusion": "failure"},
                        {"number": 3, "name": "Post", "status": "queued", "conclusion": None},
                    ],
                }
            ],
        },
    )
    detail = await harness.github.run(RunRef(project_id="hello", run_id=101))
    assert (detail.run.workflow, detail.run.outcome, detail.run.attempt) == ("CI", "failure", 1)
    (job,) = detail.jobs
    assert (job.name, job.outcome, job.duration_s, job.logs_url) == (
        "test",
        "failure",
        60,
        f"{REPO_URL}/actions/runs/101/job/900",
    )
    assert [(step.name, step.outcome) for step in job.steps] == [
        ("Set up job", "success"),
        ("pytest", "failure"),
        ("Post", "queued"),
    ]


async def test_releases_and_tags_are_listed_newest_first(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(f"{REPO_PATH}/releases").respond(
        200,
        json=[
            {
                "id": 2,
                "tag_name": "v2.0.0-rc1",
                "name": None,
                "draft": False,
                "prerelease": True,
                "html_url": f"{REPO_URL}/releases/tag/v2.0.0-rc1",
                "body": "Notes",
                "created_at": stamp(500),
                "published_at": stamp(400),
            },
            {
                "id": 1,
                "tag_name": "v1.0.0",
                "name": "One",
                "draft": True,
                "prerelease": False,
                "html_url": f"{REPO_URL}/releases/tag/v1.0.0",
                "created_at": stamp(5000),
                "published_at": None,
            },
        ],
    )
    router.get(f"{REPO_PATH}/tags").respond(
        200, json=[{"name": "v2.0.0-rc1", "commit": {"sha": "f" * 40}}, {"name": "v1.0.0", "commit": {"sha": "e" * 40}}]
    )
    result = await harness.github.releases(ProjectRef(project_id="hello"))
    assert [(r.tag, r.prerelease, r.draft) for r in result.releases] == [
        ("v2.0.0-rc1", True, False),
        ("v1.0.0", False, True),
    ]
    assert result.releases[0].published_at == pytest.approx((NOW - timedelta(seconds=400)).timestamp(), abs=1)
    assert result.releases[1].published_at is None  # a draft was never published
    assert [(t.name, t.sha) for t in result.tags] == [("v2.0.0-rc1", "f" * 10), ("v1.0.0", "e" * 10)]


async def test_deployments_come_with_environments_and_their_latest_status(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(f"{REPO_PATH}/environments").respond(
        200, json={"total_count": 1, "environments": [{"name": "production", "html_url": f"{REPO_URL}/deployments"}]}
    )
    router.get(f"{REPO_PATH}/deployments").respond(
        200,
        json=[
            {
                "id": 31,
                "environment": "production",
                "ref": "main",
                "sha": "9" * 40,
                "description": "Deploy main",
                "creator": {"login": "octocat"},
                "created_at": stamp(100),
            },
            {
                "id": 30,
                "environment": "staging",
                "ref": "dev",
                "sha": "8" * 40,
                "creator": None,
                "created_at": stamp(900),
            },
        ],
    )
    router.get(f"{REPO_PATH}/deployments/31/statuses").respond(
        200, json=[{"state": "success", "environment_url": "https://hello.example"}]
    )
    router.get(f"{REPO_PATH}/deployments/30/statuses").respond(200, json=[])
    result = await harness.github.deployments(ProjectRef(project_id="hello"))
    assert [environment.name for environment in result.environments] == ["production"]
    first, second = result.deployments
    assert (first.environment, first.state, first.environment_url, first.creator) == (
        "production",
        "success",
        "https://hello.example",
        "octocat",
    )
    assert (second.state, second.environment_url, second.creator) == (None, None, None)


async def test_repo_metadata_reports_the_fork_parent(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(REPO_PATH).respond(
        200, json=repo_json(fork=True, private=True, parent={"full_name": "upstream/Hello-World"}, archived=True)
    )
    info = await harness.github.repo(ProjectRef(project_id="hello"))
    assert (info.repo, info.fork, info.parent, info.private, info.archived) == (
        SLUG,
        True,
        "upstream/Hello-World",
        True,
        True,
    )


@pytest.mark.parametrize(
    ("status", "body", "headers", "expected_status", "message"),
    [
        (404, {"message": "Not Found"}, {}, 404, "sign in"),
        (500, {"message": "Server Error"}, {}, 502, "Server Error"),
        (403, {"message": "API rate limit exceeded"}, rate_headers(remaining=0), 429, "rate limit"),
    ],
    ids=["not-found", "server-error", "rate-limit"],
)
async def test_query_failures_become_action_errors_with_matching_statuses(
    harness: Harness,
    hello: ProjectRecord,
    router: respx.MockRouter,
    status: int,
    body: dict[str, str],
    headers: dict[str, str],
    expected_status: int,
    message: str,
) -> None:
    router.get(f"{REPO_PATH}/releases").respond(status, json=body, headers=headers)
    with pytest.raises(ActionError, match=message) as failure:
        await harness.github.releases(ProjectRef(project_id="hello"))
    assert failure.value.status == expected_status


async def test_queries_and_writes_refuse_unknown_and_non_github_projects(
    harness: Harness, make_project: Callable[..., Path], router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    await harness.track(make_project("Local", manifest=None), "local")
    with pytest.raises(ActionError, match="is not on GitHub") as local:
        await harness.github.issues(IssueListInput(project_id="local"))
    assert local.value.status == 409
    with pytest.raises(ActionError, match="is not on GitHub"):
        await harness.github.create_issue(CreateIssueInput(project_id="local", title="x"))
    with pytest.raises(ActionError, match="no project 'ghost'") as ghost:
        await harness.github.repo(ProjectRef(project_id="ghost"))
    assert ghost.value.status == 404
    with pytest.raises(ActionError, match="is not on GitHub"):
        await harness.github.refresh_now(ProjectRef(project_id="local"))
    assert router.calls.call_count == 0


# --- writes ---


async def test_creating_an_issue_posts_title_body_and_labels(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    create = router.post(f"{REPO_PATH}/issues").respond(201, json=issue_json(42, "Broken", labels=["bug"]))
    issue = await harness.github.create_issue(
        CreateIssueInput(project_id="hello", title="Broken", body="It **breaks**", labels=["bug"])
    )
    assert sent_json(create) == {"title": "Broken", "body": "It **breaks**", "labels": ["bug"]}
    assert request_of(create).headers["authorization"] == f"Bearer {SECRET}"
    assert (issue.number, issue.labels) == (42, ["bug"])
    assert harness.github._tracked["hello"].due_at == 0.0  # the poll loop is asked to look again


async def test_commenting_posts_the_markdown_to_the_issue(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    create = router.post(f"{REPO_PATH}/issues/7/comments").respond(
        201,
        json={
            "id": 77,
            "user": {"login": "octocat"},
            "body": "On it",
            "html_url": f"{REPO_URL}/issues/7#issuecomment-77",
            "created_at": stamp(0),
        },
    )
    comment = await harness.github.comment(CommentInput(project_id="hello", number=7, body="On it"))
    assert sent_json(create) == {"body": "On it"} and (comment.id, comment.author) == (77, "octocat")


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("completed", {"state": "closed", "state_reason": "completed"}),
        ("not_planned", {"state": "closed", "state_reason": "not_planned"}),
    ],
)
async def test_closing_an_issue_patches_its_state_with_the_reason(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter, reason: str, expected: dict[str, str]
) -> None:
    harness.auth.auth = SIGNED_IN
    patch = router.patch(f"{REPO_PATH}/issues/7").respond(200, json=issue_json(7, "Crash", state="closed"))
    closed = await harness.github.close_issue(
        CloseIssueInput.model_validate({"project_id": "hello", "number": 7, "reason": reason})
    )
    assert sent_json(patch) == expected and closed.state == "closed"


async def test_reopening_an_issue_patches_it_open(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    patch = router.patch(f"{REPO_PATH}/issues/7").respond(200, json=issue_json(7, "Crash"))
    reopened = await harness.github.reopen_issue(IssueRef(project_id="hello", number=7))
    assert sent_json(patch) == {"state": "open", "state_reason": "reopened"} and reopened.state == "open"


async def test_setting_labels_replaces_them_and_reports_what_github_applied(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    put = router.put(f"{REPO_PATH}/issues/7/labels").respond(200, json=[{"name": "bug"}, {"name": "p1"}])
    labels = await harness.github.set_labels(SetLabelsInput(project_id="hello", number=7, labels=["bug", "p1"]))
    assert sent_json(put) == {"labels": ["bug", "p1"]} and (labels.number, labels.labels) == (7, ["bug", "p1"])


async def test_rerunning_failed_jobs_posts_to_the_run(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    rerun = router.post(f"{REPO_PATH}/actions/runs/101/rerun-failed-jobs").respond(201, json={})
    done = await harness.github.rerun_failed_jobs(RunRef(project_id="hello", run_id=101))
    assert rerun.call_count == 1 and sent_json(rerun) == {} and done.ok
    assert harness.github._tracked["hello"].due_at == 0.0  # the run is about to be active: poll quickly


async def test_a_pull_request_defaults_to_the_repositorys_default_branch(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    router.get(REPO_PATH).respond(200, json=repo_json(default_branch="trunk"))
    create = router.post(f"{REPO_PATH}/pulls").respond(201, json=pull_json(15, SHA_A, "Add it"))
    pull = await harness.github.create_pull_request(
        CreatePullRequestInput(project_id="hello", head="feature-15", title="Add it", body="Why", draft=True)
    )
    assert sent_json(create) == {"title": "Add it", "head": "feature-15", "base": "trunk", "body": "Why", "draft": True}
    assert (pull.number, pull.head_branch, pull.checks) == (15, "feature-15", None)


async def test_a_pull_request_can_name_its_base(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    repo_route = router.get(REPO_PATH).respond(200, json=repo_json())
    create = router.post(f"{REPO_PATH}/pulls").respond(201, json=pull_json(16, SHA_A))
    await harness.github.create_pull_request(
        CreatePullRequestInput(project_id="hello", head="feature-16", base="release", title="Backport")
    )
    assert sent_json(create)["base"] == "release" and repo_route.call_count == 0


async def test_github_rejecting_a_write_surfaces_its_reason(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    router.post(f"{REPO_PATH}/issues").respond(
        422,
        json={"message": "Validation Failed", "errors": [{"resource": "Issue", "field": "labels", "code": "invalid"}]},
    )
    with pytest.raises(ActionError, match=r"Validation Failed \(labels: invalid\)") as rejected:
        await harness.github.create_issue(CreateIssueInput(project_id="hello", title="x"))
    assert rejected.value.status == 422


async def test_a_write_answer_of_the_wrong_shape_is_an_error_not_a_crash(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    router.post(f"{REPO_PATH}/issues").respond(201, json={"number": "seven"})
    with pytest.raises(ActionError, match="unexpected response") as odd:
        await harness.github.create_issue(CreateIssueInput(project_id="hello", title="x"))
    assert odd.value.status == 502


WRITES: list[tuple[str, BaseModel]] = [
    ("create_issue", CreateIssueInput(project_id="hello", title="x")),
    ("comment", CommentInput(project_id="hello", number=1, body="x")),
    ("close_issue", CloseIssueInput(project_id="hello", number=1)),
    ("reopen_issue", IssueRef(project_id="hello", number=1)),
    ("set_labels", SetLabelsInput(project_id="hello", number=1, labels=[])),
    ("rerun_failed_jobs", RunRef(project_id="hello", run_id=1)),
    ("create_pull_request", CreatePullRequestInput(project_id="hello", head="a", title="x")),
]


@pytest.mark.parametrize(("method", "request_model"), WRITES, ids=[name for name, _ in WRITES])
async def test_every_write_refuses_without_a_token_and_sends_nothing(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter, method: str, request_model: BaseModel
) -> None:
    with pytest.raises(ActionError, match="GitHub writes need a token") as refused:
        await getattr(harness.github, method)(request_model)
    assert refused.value.status == 409 and router.calls.call_count == 0


# --- the token the user saves ---


async def test_saving_a_token_stores_it_in_the_keyring_and_signs_in_without_echoing_it(
    harness: Harness,
    hello: ProjectRecord,
    router: respx.MockRouter,
    _isolated_credentials: MemoryKeyring,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness.auth.chain_runner = FakeRunner()
    mock_repository(router)
    await harness.github.refresh("hello")
    assert harness.github.state.authenticated is False
    with caplog.at_level(logging.DEBUG):
        done = await harness.github.save_token(SaveTokenInput(token=SecretStr(f"  {SECRET}\n")))
    assert _isolated_credentials.secrets == {(github_auth.KEYRING_SERVICE, github_auth.KEYRING_USERNAME): SECRET}
    assert (harness.github.state.authenticated, harness.github.state.auth_source) == (True, AuthSource.KEYRING)
    assert done.message == "token saved; GitHub access now uses the token saved in the OS keyring"
    assert SECRET not in done.model_dump_json() and SECRET not in caplog.text
    assert harness.github._tracked["hello"].due_at == 0.0
    routes = mock_repository(router)
    graphql = router.post("/graphql").respond(
        200, json={"data": {"repository": {"pullRequests": {"totalCount": 0, "nodes": []}}}}
    )
    await harness.github.refresh("hello")
    assert request_of(routes["repo"]).headers["authorization"] == f"Bearer {SECRET}"
    assert request_of(graphql).headers["authorization"] == f"Bearer {SECRET}"


async def test_forgetting_the_saved_token_signs_out_when_no_other_source_exists(
    harness: Harness, hello: ProjectRecord, _isolated_credentials: MemoryKeyring
) -> None:
    harness.auth.chain_runner = FakeRunner()
    await harness.github.save_token(SaveTokenInput(token=SecretStr(SECRET)))
    done = await harness.github.clear_token(NoInput())
    assert done.message == "saved token removed; GitHub access now uses no token"
    assert _isolated_credentials.secrets == {} and harness.github.state.authenticated is False
    again = await harness.github.clear_token(NoInput())
    assert again.message is not None and again.message.startswith("no saved token")


async def test_a_missing_keyring_makes_token_actions_fail_with_a_clear_error(
    harness: Harness, hello: ProjectRecord
) -> None:
    keyring.set_keyring(fail.Keyring())  # type: ignore[no-untyped-call]  # untyped in keyring
    with pytest.raises(ActionError, match="could not store the token") as save:
        await harness.github.save_token(SaveTokenInput(token=SecretStr(SECRET)))
    with pytest.raises(ActionError, match="could not remove the token"):
        await harness.github.clear_token(NoInput())
    assert save.value.status == 500 and SECRET not in save.value.message


# --- secrecy and self-description ---


async def test_the_token_reaches_no_state_event_or_log(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    harness.auth.auth = SIGNED_IN
    mock_repository(router)
    router.post("/graphql").respond(
        200, json={"data": {"repository": {"pullRequests": {"totalCount": 0, "nodes": []}}}}
    )
    with caplog.at_level(logging.DEBUG):
        await harness.github.refresh("hello")
    published = [
        harness.github.state.model_dump_json(),
        harness.runtime.states.snapshot("github").model_dump_json(),
        *[envelope.model_dump_json() for envelope in harness.drain_events()],
        *[facts.model_dump_json() for _project, facts in harness.facts],
        caplog.text,
    ]
    assert harness.github.state.authenticated and all(SECRET not in text for text in published)


def test_every_action_is_declared_with_the_right_flags() -> None:
    specs = {action_id: meta for action_id, meta, _input, _output in GitHubService.action_specs()}
    assert {action_id for action_id, meta in specs.items() if meta.query} == {
        "repo",
        "issues",
        "issue",
        "pulls",
        "runs",
        "run",
        "releases",
        "deployments",
    }
    assert {action_id for action_id, meta in specs.items() if meta.confirm} == {
        "close-issue",
        "rerun-failed-jobs",
        "clear-token",
    }
    assert {action_id for action_id, meta in specs.items() if not meta.query} >= {
        "create-issue",
        "comment",
        "reopen-issue",
        "set-labels",
        "create-pull-request",
        "save-token",
        "clear-token",
        "refresh-now",
    }
    assert not any(meta.long_running or meta.danger for meta in specs.values())


def test_forms_are_described_for_the_schema_renderer() -> None:
    create = CreateIssueInput.model_json_schema()["properties"]
    assert create["project_id"]["x-ui"] == {"hidden": True, "bind": "project"}
    assert create["body"]["x-ui"]["widget"] == "textarea" and create["labels"]["x-ui"]["widget"] == "tags"
    token = SaveTokenInput.model_json_schema()["properties"]["token"]
    assert token["x-ui"]["widget"] == "secret" and token["format"] == "password"
    state = GitHubService.State.model_json_schema()["properties"]
    assert state["auth_source"]["x-ui"]["format"] == "badge" and "x-ui" in state["authenticated"]


# --- the rules that turn payloads into facts ---


def workflows_of(*ids: int) -> list[WorkflowPayload]:
    return [
        WorkflowPayload.model_validate(workflow_json(workflow_id, f"Workflow {workflow_id}")) for workflow_id in ids
    ]


def runs_of(*runs: dict[str, object]) -> list[RunPayload]:
    return [RunPayload.model_validate(run) for run in runs]


@pytest.mark.parametrize(
    ("latest", "conclusion"),
    [
        ([], "none"),
        ([run_json(1, 1, conclusion="success"), run_json(2, 2, conclusion="skipped")], "success"),
        ([run_json(1, 1, conclusion="success"), run_json(2, 2, conclusion="failure")], "failure"),
        ([run_json(1, 1, conclusion="timed_out"), run_json(2, 2, status="in_progress", conclusion=None)], "failure"),
        ([run_json(1, 1, conclusion="success"), run_json(2, 2, status="queued", conclusion=None)], "in_progress"),
        ([run_json(1, 1, conclusion="cancelled"), run_json(2, 2, conclusion="success")], "success"),
        ([run_json(1, 1, conclusion="cancelled")], "cancelled"),
        ([run_json(1, 1, conclusion="stale")], "none"),
        ([run_json(9, 9, conclusion="failure")], "none"),
    ],
    ids=[
        "no-runs",
        "skipped-counts-as-passing",
        "failure-wins-over-success",
        "failure-wins-over-running",
        "running-wins-over-success",
        "cancelled-hides-behind-success",
        "only-cancelled",
        "unknown-conclusion-is-ignored",
        "run-of-a-workflow-that-is-not-listed",
    ],
)
def test_ci_conclusion_across_workflows(latest: list[dict[str, object]], conclusion: str) -> None:
    facts = ci_facts(workflows_of(1, 2), runs_of(*latest), "none")
    assert facts.conclusion == conclusion and facts.workflows == 2


def test_ci_facts_name_failing_workflows_and_the_newest_run() -> None:
    facts = ci_facts(
        workflows_of(1, 2, 3),
        runs_of(
            run_json(10, 3, conclusion="failure", ago=500),
            run_json(11, 1, conclusion="failure", ago=100),
            run_json(12, 2),
        ),
        "passing",
    )
    assert facts.failing_workflows == ["Workflow 1", "Workflow 3"] and facts.open_pr_checks == "passing"
    assert facts.last_run_url == f"{REPO_URL}/actions/runs/11"  # started last: 100s ago against 300s and 500s


@pytest.mark.parametrize(
    ("check_runs", "status", "expected"),
    [
        ([], {"state": "pending", "total_count": 0}, "none"),
        ([{"status": "completed", "conclusion": "success"}], {"state": "pending", "total_count": 0}, "passing"),
        ([], {"state": "success", "total_count": 2}, "passing"),
        ([{"status": "in_progress", "conclusion": None}], {"state": "pending", "total_count": 0}, "pending"),
        ([], {"state": "pending", "total_count": 1}, "pending"),
        ([{"status": "completed", "conclusion": "cancelled"}], {"state": "pending", "total_count": 0}, "failing"),
        ([{"status": "completed", "conclusion": "success"}], {"state": "failure", "total_count": 1}, "failing"),
        ([{"status": "completed", "conclusion": "neutral"}], {"state": "error", "total_count": 1}, "failing"),
    ],
)
def test_a_commit_combines_check_runs_and_the_legacy_status(
    check_runs: list[dict[str, object]], status: dict[str, object], expected: str
) -> None:
    runs = [CheckRunPayload.model_validate(run) for run in check_runs]
    assert checks_from_check_runs(runs, CombinedStatusPayload.model_validate(status)) == expected


def test_the_worst_pull_request_checks_win_and_unfetched_ones_do_not_count() -> None:
    assert combine_checks(["passing", None, "pending"]) == "pending"
    assert combine_checks(["passing", "failing", "pending"]) == "failing"
    assert combine_checks(["passing", None]) == "passing"
    assert combine_checks([None, "none"]) == "none"
    assert combine_checks([]) == "none"


def test_top_labels_rank_by_count_then_name() -> None:
    issues = [
        IssuePayload.model_validate(issue_json(n, f"Issue {n}", labels=labels))
        for n, labels in enumerate([["docs", "bug"], ["bug", "Zed"], ["alpha"], ["bug"], ["docs"]], start=1)
    ]
    assert top_labels(issues) == ["bug", "docs", "alpha"]
    assert top_labels(issues, limit=1) == ["bug"]


# --- details of failure handling ---


async def test_a_json_error_body_that_is_not_an_object_still_yields_a_message(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    router.get(f"{REPO_PATH}/releases").respond(500, json=["boom"])
    with pytest.raises(ActionError, match=r"\(HTTP 500\)") as failure:
        await harness.github.releases(ProjectRef(project_id="hello"))
    assert failure.value.status == 502


async def test_a_rejected_credential_on_a_query_asks_for_a_new_one(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    harness.auth.auth = SIGNED_IN
    router.get(f"{REPO_PATH}/releases").respond(401, json={"message": "Bad credentials"})
    with pytest.raises(ActionError, match="GitHub rejected the token from GH_TOKEN or GITHUB_TOKEN") as failure:
        await harness.github.releases(ProjectRef(project_id="hello"))
    assert failure.value.status == 409


async def test_concurrent_first_uses_build_one_client_and_resolve_the_chain_once(
    harness: Harness, hello: ProjectRecord
) -> None:
    first, second = await asyncio.gather(harness.github._api(), harness.github._api())
    assert first is second and harness.auth.calls == 1


async def test_a_manifest_change_polls_the_repo_again_but_a_git_change_alone_does_not(
    harness: Harness, hello: ProjectRecord, router: respx.MockRouter
) -> None:
    mock_repository(router)
    await harness.github.refresh("hello")
    tracked = harness.github._tracked["hello"]
    await harness.github.files_changed(hello, frozenset({"git", "worktree"}))
    assert tracked.due_at > 0.0
    await harness.github.files_changed(hello, frozenset({"manifest"}))
    assert tracked.due_at == 0.0
    await harness.github.refresh("hello")
    await harness.github.project_upserted(hello)
    assert tracked.due_at == 0.0


async def test_asking_for_one_project_leaves_the_others_waiting(
    harness: Harness,
    hello: ProjectRecord,
    make_project: Callable[..., Path],
    git: Git,
    router: respx.MockRouter,
) -> None:
    mock_repository(router)
    other = make_project("Other", manifest=None)
    git(other, "remote", "add", "origin", "https://github.com/octocat/Spoon-Knife.git")
    await harness.track(other, "other")
    harness.github._tracked["other"].due_at = time.monotonic() + 1000
    await harness.github.refresh_now(ProjectRef(project_id="hello"))
    assert harness.github._tracked["hello"].due_at == 0.0
    assert harness.github._tracked["other"].due_at > 0.0


async def test_a_blank_token_is_refused_and_nothing_is_stored(
    harness: Harness, hello: ProjectRecord, _isolated_credentials: MemoryKeyring
) -> None:
    with pytest.raises(ActionError, match="the token is empty") as blank:
        await harness.github.save_token(SaveTokenInput(token=SecretStr(" \n")))
    assert blank.value.status == 422 and _isolated_credentials.secrets == {}
    with pytest.raises(ValidationError):
        SaveTokenInput(token=SecretStr(""))


def test_the_service_is_served_over_http_with_typed_models(
    data_paths: DataPaths,
    make_project: Callable[..., Path],
    git: Git,
    router: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_credentials: MemoryKeyring,
) -> None:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    projects = ProjectsService(runtime, DiscoveryService(runtime, jobs), ManifestsService(runtime))
    service = GitHubService(runtime, projects, jobs)
    runtime.register([jobs, projects, service])
    stub = AuthStub()
    stub.chain_runner = FakeRunner()
    monkeypatch.setattr(github_service, "resolve_auth", stub)
    mock_repository(router)
    router.get(REPO_PATH).respond(200, json=repo_json(fork=True, parent={"full_name": "upstream/Hello-World"}))
    path = make_project("Hello", manifest=None)
    git(path, "remote", "add", "origin", f"{REPO_URL}.git")
    record = ProjectRecord("hello", path, "managed", None, (), False, True)
    with TestClient(create_app(runtime), base_url="http://127.0.0.1") as client:
        assert client.portal is not None
        client.portal.call(service.project_upserted, record)
        descriptor = {d["id"]: d for d in client.get("/api/services").json()}["github"]
        assert {a["id"] for a in descriptor["actions"] if a["confirm"]} == {
            "close-issue",
            "rerun-failed-jobs",
            "clear-token",
        }
        assert [e["type"] for e in descriptor["events"]] == [
            "github.ci-failure",
            "github.ci-run-completed",
            "github.issue-opened",
        ]
        repo = client.post("/api/services/github/actions/repo", json={"project_id": "hello"})
        assert repo.status_code == 200
        assert repo.json() == {
            "repo": SLUG,
            "html_url": REPO_URL,
            "description": "My first repository",
            "default_branch": "main",
            "private": False,
            "fork": True,
            "parent": "upstream/Hello-World",
            "archived": False,
        }
        unknown = client.post("/api/services/github/actions/issues", json={"project_id": "ghost"})
        assert (unknown.status_code, unknown.json()) == (404, {"detail": "no project 'ghost'"})
        assert client.post("/api/services/github/actions/save-token", json={"token": ""}).status_code == 422
        saved = client.post("/api/services/github/actions/save-token", json={"token": SECRET})
        assert saved.status_code == 200 and SECRET not in saved.text
        state = client.get("/api/services/github/state").json()["state"]
        assert (state["authenticated"], state["auth_source"]) == (True, "keyring") and SECRET not in json.dumps(state)
        assert "SaveTokenInput" in client.get("/openapi.json").json()["components"]["schemas"]
