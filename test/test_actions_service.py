"""Manifest custom actions, param validation, service requirements, and cleaning build artifacts."""

import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import MANIFEST, Git
from testing_support import wait_for

from forge_dashboard.core.events import EventEnvelope
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError
from forge_dashboard.services import actions as actions_module
from forge_dashboard.services.actions import (
    ActionsConfig,
    ActionsService,
    ArtifactScope,
    CleanInput,
    HistoryInput,
    RunActionInput,
    artifact_target,
    directory_size,
    find_artifact_dirs,
    human_bytes,
)
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.jobs import JobIdInput, JobLogInput, JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService

PYTHON = sys.executable.replace(chr(92), "/")

SCRIPT = """
import json
import os
import pathlib
import sys

mode = sys.argv[1]
result_path = pathlib.Path(os.environ["FORGE_ACTION_RESULT"])
if mode == "greet":
    params = json.load(sys.stdin)
    result = {
        "summary": f"greeted {params['name']} x{params.get('times', 1)}",
        "links": [{"label": "docs", "url": "http://localhost/docs"}],
        "artifacts": ["out/greeting.txt"],
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")
    print("project", os.environ["FORGE_PROJECT_ID"], pathlib.Path(os.environ["FORGE_PROJECT_ROOT"]).is_dir())
    print("extra", os.environ["EXTRA"])
elif mode == "plain":
    print("stdin", repr(sys.stdin.read()))
elif mode == "env":
    print("url", os.environ.get("FORGE_SERVICE_URL_MY_API"))
elif mode == "bad-result":
    result_path.write_text('{"summary": 5, "unexpected": true}', encoding="utf-8")
elif mode == "fail":
    result_path.write_text('{"summary": "disk full"}', encoding="utf-8")
    sys.exit(3)
"""

ACTIONS = f"""
[[services]]
id = "my-api"
label = "API"
argv = ["{PYTHON}", "-c", "pass"]

[[actions]]
id = "greet"
label = "Greet"
description = "Say hello"
argv = ["{PYTHON}", "scripts/act.py", "greet"]
env = {{ EXTRA = "{{project_root}}/extra" }}
timeout_s = 60

[actions.params]
type = "object"
required = ["name"]
additionalProperties = false

[actions.params.properties.name]
type = "string"
minLength = 2

[actions.params.properties.times]
type = "integer"
minimum = 1

[[actions]]
id = "plain"
label = "Plain"
argv = ["{PYTHON}", "scripts/act.py", "plain"]

[[actions]]
id = "needs-api"
label = "Needs API"
argv = ["{PYTHON}", "scripts/act.py", "env"]
requires_service = "my-api"
confirm = true

[[actions]]
id = "bad-result"
label = "Bad result"
argv = ["{PYTHON}", "scripts/act.py", "bad-result"]

[[actions]]
id = "fail"
label = "Fail"
argv = ["{PYTHON}", "scripts/act.py", "fail"]
danger = true

[[actions]]
id = "slow"
label = "Slow"
argv = ["{PYTHON}", "-c", "import time; time.sleep(30)"]
timeout_s = 1

[[actions]]
id = "ghost"
label = "Ghost"
argv = ["no-such-program-for-forge-dashboard-tests"]
"""


@dataclass
class Env:
    runtime: Runtime
    actions: ActionsService
    projects: ProjectsService
    jobs: JobsService
    discovery: DiscoveryService
    running_services: dict[tuple[str, str], str]

    async def add_project(self, root: Path) -> None:
        await self.discovery.add_project(PathInput(path=str(root)))
        await wait_for(lambda: any(record.path == root for record in self.projects.records()))

    async def run(self, action_id: str, params: dict[str, Any] | None = None, project_id: str = "alpha") -> str:
        """Run an action to completion and return its job id."""
        ref = await self.actions.run(RunActionInput(project_id=project_id, action_id=action_id, params=params or {}))
        await self.jobs.wait(ref.job_id)
        return ref.job_id

    async def log(self, job_id: str) -> list[str]:
        return (await self.jobs.log(JobLogInput(job_id=job_id))).lines

    async def result(self, job_id: str) -> dict[str, Any]:
        detail = await self.jobs.get(JobIdInput(job_id=job_id))
        assert detail.result is not None
        return dict(detail.result)


@pytest.fixture
async def env(data_paths: DataPaths) -> AsyncIterator[Env]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    running_services: dict[tuple[str, str], str] = {}
    actions = ActionsService(
        runtime, projects, jobs, lambda project_id, service_id: running_services.get((project_id, service_id))
    )
    runtime.register([jobs, manifests, projects, actions, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(data_paths.root.parent / "no-projects-here")]})
    await runtime.start()
    try:
        yield Env(runtime, actions, projects, jobs, discovery, running_services)
    finally:
        await runtime.stop()


BUILD_OUTPUT_IGNORES = ".venv/\nnode_modules/\ntarget/\n__pycache__/\n.pytest_cache/\ndist/\ncoverage/\nbuild/\n"


@pytest.fixture
async def alpha(env: Env, make_project: Callable[..., Path]) -> Path:
    extra = {"scripts/act.py": SCRIPT, ".gitignore": BUILD_OUTPUT_IGNORES}
    root = make_project("Alpha", manifest=MANIFEST + ACTIONS, extra=extra)
    await env.add_project(root)
    await wait_for(lambda: bool(env.actions.state.projects))
    return root


# --- custom actions ---


async def test_state_lists_each_projects_actions_with_the_params_schema_for_the_form(env: Env, alpha: Path) -> None:
    [project] = env.actions.state.projects
    actions = {info.id: info for info in project.actions}

    assert project.project_id == "alpha" and list(actions) == [
        "greet",
        "plain",
        "needs-api",
        "bad-result",
        "fail",
        "slow",
        "ghost",
    ]
    greet = actions["greet"]
    assert greet.label == "Greet" and greet.description == "Say hello"
    assert greet.params is not None and greet.params["required"] == ["name"]
    assert (actions["plain"].params, actions["needs-api"].requires_service) == (None, "my-api")
    assert (actions["needs-api"].confirm, actions["fail"].danger, actions["greet"].danger) == (True, True, False)


async def test_a_project_that_stops_declaring_actions_leaves_the_list(env: Env, alpha: Path, git: Git) -> None:
    (alpha / "forge.project.toml").write_text(
        MANIFEST.replace("{name}", "Alpha").replace("{project_id}", "alpha"), encoding="utf-8"
    )
    await env.projects.refresh("alpha")

    await wait_for(lambda: env.actions.state.projects == [])


async def test_a_removed_project_leaves_the_list(env: Env, alpha: Path) -> None:
    await env.actions.project_removed("alpha")

    assert env.actions.state.projects == []


async def test_run_sends_params_on_stdin_and_returns_the_result_file(env: Env, alpha: Path) -> None:
    job_id = await env.run("greet", {"name": "Ada", "times": 2})

    summary = await env.jobs.wait(job_id)
    assert (summary.status, summary.summary) == (JobStatus.SUCCEEDED, "greeted Ada x2")
    lines = [line.replace(chr(92), "/") for line in await env.log(job_id)]
    assert "project alpha True" in lines
    assert any(line.startswith("extra ") and line.endswith("/projects/Alpha/extra") for line in lines)
    result = await env.result(job_id)
    assert result["action_id"] == "greet" and result["exit_code"] == 0 and result["timed_out"] is False
    assert result["result"] == {
        "summary": "greeted Ada x2",
        "links": [{"label": "docs", "url": "http://localhost/docs"}],
        "artifacts": ["out/greeting.txt"],
    }


async def test_an_action_without_a_params_schema_gets_an_empty_stdin(env: Env, alpha: Path) -> None:
    job_id = await env.run("plain")

    assert "stdin ''" in await env.log(job_id)
    assert (await env.jobs.wait(job_id)).summary == "done"


async def test_params_are_validated_against_the_schema_before_anything_runs(env: Env, alpha: Path) -> None:
    cases = {
        "required": ({}, "params: 'name' is a required property"),
        "type": ({"name": "Ada", "times": "twice"}, "times: 'twice' is not of type 'integer'"),
        "short": ({"name": "A"}, "name: 'A' is too short"),
        "extra": ({"name": "Ada", "colour": "red"}, "Additional properties are not allowed"),
    }
    for params, message in cases.values():
        with pytest.raises(ActionError, match=message) as failure:
            await env.actions.run(RunActionInput(project_id="alpha", action_id="greet", params=params))
        assert failure.value.status == 422
    with pytest.raises(ActionError, match="takes no params") as unexpected:
        await env.actions.run(RunActionInput(project_id="alpha", action_id="plain", params={"x": 1}))
    assert unexpected.value.status == 422
    assert (await env.actions.history(HistoryInput(project_id="alpha"))).jobs == []


async def test_every_schema_violation_is_reported_together(env: Env, alpha: Path) -> None:
    with pytest.raises(ActionError) as failure:
        await env.actions.run(
            RunActionInput(project_id="alpha", action_id="greet", params={"name": "A", "times": 0, "x": 1})
        )
    message = failure.value.message
    assert "name: 'A' is too short" in message and "times: 0 is less than the minimum of 1" in message
    assert "Additional properties" in message


async def test_a_service_requirement_that_is_not_running_is_refused_with_a_conflict(env: Env, alpha: Path) -> None:
    with pytest.raises(ActionError, match="needs the service 'my-api' to be running; start it first") as failure:
        await env.actions.run(RunActionInput(project_id="alpha", action_id="needs-api"))
    assert failure.value.status == 409
    assert (await env.actions.history(HistoryInput(project_id="alpha"))).jobs == []


async def test_a_running_required_service_is_exported_as_an_environment_variable(env: Env, alpha: Path) -> None:
    env.running_services[("alpha", "my-api")] = "http://127.0.0.1:5555"

    job_id = await env.run("needs-api")

    assert "url http://127.0.0.1:5555" in await env.log(job_id)


async def test_the_service_url_variable_is_named_by_the_env_key_project_services_also_uses(
    env: Env, alpha: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actions_module, "env_key", lambda service_id: f"RENAMED_{service_id}")
    env.running_services[("alpha", "my-api")] = "http://127.0.0.1:5555"

    job_id = await env.run("needs-api")

    assert "url None" in await env.log(job_id), "the script looks for FORGE_SERVICE_URL_MY_API, which is no longer set"


async def test_a_failing_action_fails_the_job_and_keeps_the_result_summary(env: Env, alpha: Path) -> None:
    async with env.runtime.bus.subscribe() as events:
        job_id = await env.run("fail")
        published = [events.get_nowait() for _ in range(events.qsize())]

    summary = await env.jobs.wait(job_id)
    assert (summary.status, summary.summary) == (JobStatus.FAILED, "exit 3: disk full")
    [ran] = [event for event in published if event.type == "actions.ran"]
    assert (ran.severity, ran.title, ran.project_id) == ("error", "Ran Fail: exit 3: disk full", "alpha")


async def test_an_action_that_overruns_its_timeout_is_killed_and_fails(env: Env, alpha: Path) -> None:
    job_id = await env.run("slow")

    summary = await env.jobs.wait(job_id)
    assert (summary.status, summary.summary) == (JobStatus.FAILED, "timed out after 1s")
    assert (await env.result(job_id))["timed_out"] is True


async def test_a_missing_executable_fails_the_job_with_a_clear_message(env: Env, alpha: Path) -> None:
    job_id = await env.run("ghost")

    summary = await env.jobs.wait(job_id)
    assert summary.status is JobStatus.FAILED
    assert summary.summary == "'no-such-program-for-forge-dashboard-tests' is not on PATH"


async def test_an_invalid_result_file_is_logged_and_ignored_when_the_exit_code_says_success(
    env: Env, alpha: Path
) -> None:
    job_id = await env.run("bad-result")

    summary = await env.jobs.wait(job_id)
    assert (summary.status, summary.summary) == (JobStatus.SUCCEEDED, "done")
    result = await env.result(job_id)
    assert result["result"] is None
    assert "summary: Input should be a valid string" in result["result_error"]
    assert any(line.startswith("result file ignored:") for line in await env.log(job_id))


async def test_a_finished_action_publishes_a_persisted_project_event(env: Env, alpha: Path) -> None:
    async with env.runtime.bus.subscribe() as events:
        job_id = await env.run("greet", {"name": "Ada"})
        envelopes: list[EventEnvelope] = [events.get_nowait() for _ in range(events.qsize())]

    [ran] = [envelope for envelope in envelopes if envelope.type == "actions.ran"]
    assert (ran.service, ran.project_id, ran.persist, ran.severity) == ("actions", "alpha", True, "success")
    assert ran.title == "Ran Greet: greeted Ada x1"
    assert ran.dedupe_key == f"actions:{job_id}"
    assert ran.data["action_id"] == "greet" and ran.data["succeeded"] is True


async def test_unknown_actions_and_projects_without_manifests_are_refused(
    env: Env, alpha: Path, make_project: Callable[..., Path]
) -> None:
    with pytest.raises(ActionError, match="no action 'nope'") as unknown_action:
        await env.run("nope")
    with pytest.raises(ActionError) as unknown_project:
        await env.run("greet", {"name": "Ada"}, project_id="nobody")
    await env.add_project(make_project("Bare", manifest=None))
    with pytest.raises(ActionError, match="no valid manifest") as no_manifest:
        await env.run("greet", {"name": "Ada"}, project_id="bare")
    assert (unknown_action.value.status, unknown_project.value.status, no_manifest.value.status) == (404, 404, 409)


async def test_history_lists_the_projects_action_jobs_newest_first(env: Env, alpha: Path) -> None:
    first = await env.run("plain")
    (alpha / "dist").mkdir()
    (alpha / "dist" / "bundle.js").write_text("x" * 10, encoding="utf-8")
    clean = await env.actions.clean_artifacts(CleanInput(project_id="alpha", paths=["dist"]))
    await env.jobs.wait(clean.job_id)
    third = await env.run("plain")

    history = await env.actions.history(HistoryInput(project_id="alpha"))
    assert [job.id for job in history.jobs] == [third, clean.job_id, first]
    assert [job.kind for job in history.jobs] == ["actions.run", "actions.clean", "actions.run"]
    assert [job.id for job in (await env.actions.history(HistoryInput(project_id="alpha", limit=1))).jobs] == [third]
    assert (await env.actions.history(HistoryInput(project_id="other"))).jobs == []


# --- artifacts ---


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


@pytest.fixture
def artifact_tree(alpha: Path) -> Path:
    _write(alpha / ".venv" / "lib" / "site.bin", 1000)
    _write(alpha / ".venv" / "node_modules" / "ignored-inside-an-artifact.js", 99)
    _write(alpha / "web" / "node_modules" / "pkg" / "index.js", 500)
    _write(alpha / "web" / "dist" / "app.js", 300)
    _write(alpha / "src" / "pkg" / "sub" / "deeper" / "__pycache__" / "m.pyc", 40)
    _write(alpha / "src" / "pkg" / "__pycache__" / "n.pyc", 30)
    _write(alpha / ".pytest_cache" / "v" / "cache", 20)
    _write(alpha / "a" / "b" / "c" / "node_modules" / "too-deep.js", 7)
    _write(alpha / "target" / "debug" / "bin", 2000)
    _write(alpha / "src" / "main.py", 5)
    return alpha


async def test_artifact_sizes_reports_build_output_largest_first(env: Env, artifact_tree: Path) -> None:
    sizes = await env.actions.artifact_sizes(ArtifactScope(project_id="alpha"))

    assert [(artifact.path, artifact.kind, artifact.size_bytes) for artifact in sizes.artifacts] == [
        ("target", "target", 2000),
        (".venv", ".venv", 1099),
        ("web/node_modules", "node_modules", 500),
        ("web/dist", "dist", 300),
        ("src/pkg/sub/deeper/__pycache__", "__pycache__", 40),
        ("src/pkg/__pycache__", "__pycache__", 30),
        (".pytest_cache", ".pytest_cache", 20),
    ]
    assert sizes.total_bytes == 2000 + 1099 + 500 + 300 + 40 + 30 + 20 and sizes.project_id == "alpha"


async def test_a_directory_with_tracked_files_is_not_an_artifact(env: Env, artifact_tree: Path, git: Git) -> None:
    _write(artifact_tree / "build" / "recipe.txt", 12)
    _write(artifact_tree / "build" / "scratch.o", 700)
    git(artifact_tree, "add", "-f", "build/recipe.txt")
    git(artifact_tree, "commit", "-q", "-m", "Track a build recipe")
    _write(artifact_tree / "coverage" / "lcov.info", 50)

    paths = [
        artifact.path for artifact in (await env.actions.artifact_sizes(ArtifactScope(project_id="alpha"))).artifacts
    ]

    assert "build" not in paths and "coverage" in paths


async def test_links_are_neither_followed_nor_reported(env: Env, artifact_tree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "huge.bin", 100_000)
    link = artifact_tree / "packages" / "node_modules"
    link.parent.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs privileges on this machine")
    (artifact_tree / "dist").symlink_to(outside, target_is_directory=True)
    (artifact_tree / "target" / "linked").symlink_to(outside, target_is_directory=True)

    sizes = await env.actions.artifact_sizes(ArtifactScope(project_id="alpha"))

    paths = {artifact.path: artifact.size_bytes for artifact in sizes.artifacts}
    assert "packages/node_modules" not in paths and "dist" not in paths
    assert paths["target"] == 2000, "a link inside an artifact adds nothing"


async def test_extra_artifact_directories_come_from_the_config(env: Env, artifact_tree: Path) -> None:
    _write(artifact_tree / "web" / ".next" / "cache", 64)
    (artifact_tree / ".gitignore").write_text(BUILD_OUTPUT_IGNORES + ".next/\n", encoding="utf-8")
    await env.actions.update_config(ActionsConfig(extra_artifact_dirs=[".next"]))

    paths = [
        artifact.path for artifact in (await env.actions.artifact_sizes(ArtifactScope(project_id="alpha"))).artifacts
    ]

    assert "web/.next" in paths


@pytest.mark.parametrize("name", [".git", "", "..", "a/b", "a\\b"])
def test_the_config_refuses_names_that_are_not_plain_directories(name: str) -> None:
    with pytest.raises(ValueError, match="plain directory name"):
        ActionsConfig(extra_artifact_dirs=[name])


async def _sizes(env: Env, project_id: str = "alpha", *, allow_without_git: bool = False) -> list[str]:
    scope = ArtifactScope(project_id=project_id, allow_without_git=allow_without_git)
    return [artifact.path for artifact in (await env.actions.artifact_sizes(scope)).artifacts]


@pytest.fixture
async def plain(env: Env, tmp_path: Path) -> Path:
    """A project that is not a git repository, with build output next to its source."""
    root = tmp_path / "plain-project"
    _write(root / "node_modules" / "x.js", 11)
    _write(root / "dist" / "new_source.js", 3)
    (root / "forge.project.toml").write_text(
        MANIFEST.replace("{name}", "Plain").replace("{project_id}", "plain"), encoding="utf-8"
    )
    await env.add_project(root)
    return root


async def test_a_project_without_git_offers_nothing_unless_the_request_opts_in(env: Env, plain: Path) -> None:
    assert await _sizes(env, "plain") == []

    sizes = await env.actions.artifact_sizes(ArtifactScope(project_id="plain", allow_without_git=True))
    assert [(artifact.path, artifact.size_bytes) for artifact in sizes.artifacts] == [("node_modules", 11), ("dist", 3)]


async def test_cleaning_a_project_without_git_needs_the_same_opt_in(env: Env, plain: Path) -> None:
    with pytest.raises(ActionError, match="not reported by artifact sizes") as refused:
        await env.actions.clean_artifacts(CleanInput(project_id="plain", paths=["node_modules"]))
    assert refused.value.status == 422
    assert (plain / "node_modules").is_dir()

    cleaned = CleanInput(project_id="plain", paths=["node_modules"], allow_without_git=True)
    summary = await env.jobs.wait((await env.actions.clean_artifacts(cleaned)).job_id)

    assert summary.status is JobStatus.SUCCEEDED
    assert not (plain / "node_modules").exists() and (plain / "dist" / "new_source.js").is_file()


def test_the_opt_in_is_explained_in_the_forms_schema_and_off_by_default() -> None:
    for model in (ArtifactScope, CleanInput):
        field = model.model_json_schema()["properties"]["allow_without_git"]
        assert field["default"] is False
        assert "not a git repository" in field["x-ui"]["help"]
    assert CleanInput(project_id="alpha", paths=["dist"]).allow_without_git is False


async def test_a_directory_git_does_not_ignore_is_not_offered(env: Env, artifact_tree: Path) -> None:
    assert "web/dist" in await _sizes(env)

    (artifact_tree / ".gitignore").write_text(BUILD_OUTPUT_IGNORES.replace("dist/\n", ""), encoding="utf-8")

    paths = await _sizes(env)
    assert "web/dist" not in paths and "target" in paths


async def test_new_source_beside_ignored_output_keeps_its_directory_out_of_the_offer(
    env: Env, artifact_tree: Path
) -> None:
    (artifact_tree / ".gitignore").write_text("*.o\n", encoding="utf-8")
    _write(artifact_tree / "build" / "main.o", 40)
    _write(artifact_tree / "build" / "new_module.py", 10)
    _write(artifact_tree / "dist" / "only.o", 5)

    paths = await _sizes(env)

    assert "build" not in paths, "an untracked, unignored file is work git would show as new"
    assert "dist" in paths, "every file in it is ignored"


async def test_a_cache_that_ignores_itself_is_offered_whatever_the_project_gitignore_says(
    env: Env, artifact_tree: Path
) -> None:
    (artifact_tree / ".gitignore").write_text("", encoding="utf-8")
    _write(artifact_tree / ".mypy_cache" / ".gitignore", 1)
    (artifact_tree / ".mypy_cache" / ".gitignore").write_text("*\n", encoding="utf-8")
    _write(artifact_tree / ".mypy_cache" / "3.12" / "m.json", 8)

    paths = await _sizes(env)

    assert ".mypy_cache" in paths and "target" not in paths


async def test_rules_in_the_repositorys_exclude_file_count_as_ignored(env: Env, artifact_tree: Path) -> None:
    (artifact_tree / ".gitignore").write_text("", encoding="utf-8")
    (artifact_tree / ".git" / "info" / "exclude").write_text("target/\n", encoding="utf-8")

    paths = await _sizes(env)

    assert "target" in paths and "web/dist" not in paths


async def test_clean_refuses_a_directory_git_does_not_ignore(env: Env, artifact_tree: Path) -> None:
    (artifact_tree / ".gitignore").write_text("", encoding="utf-8")

    with pytest.raises(ActionError, match="not reported by artifact sizes") as refused:
        await env.actions.clean_artifacts(CleanInput(project_id="alpha", paths=["target"]))

    assert refused.value.status == 422 and (artifact_tree / "target" / "debug" / "bin").is_file()


async def test_a_nested_repository_and_its_build_output_are_left_alone(env: Env, artifact_tree: Path, git: Git) -> None:
    _write(artifact_tree / "vendor" / "lib" / "node_modules" / "dep.js", 4)
    git.init(artifact_tree / "vendor" / "lib")
    _write(artifact_tree / "dist" / ".keep", 1)
    git.init(artifact_tree / "dist")

    paths = await _sizes(env)

    assert "vendor/lib/node_modules" not in paths and "dist" not in paths and "target" in paths


def test_artifact_search_skips_nested_repositories(tmp_path: Path) -> None:
    for relative in ("node_modules", "sub/node_modules", "clone/node_modules", "dist"):
        (tmp_path / relative).mkdir(parents=True)
    (tmp_path / "clone" / ".git").mkdir()
    (tmp_path / "dist" / ".git").write_text("gitdir: ../elsewhere", encoding="utf-8")

    found = {path.relative_to(tmp_path).as_posix() for path in find_artifact_dirs(tmp_path, ["node_modules", "dist"])}

    assert found == {"node_modules", "sub/node_modules"}


async def test_clean_deletes_only_the_chosen_directories_and_reports_what_was_freed(
    env: Env, artifact_tree: Path
) -> None:
    ref = await env.actions.clean_artifacts(CleanInput(project_id="alpha", paths=["target", "web/dist", "target"]))
    summary = await env.jobs.wait(ref.job_id)

    assert (summary.status, summary.summary) == (JobStatus.SUCCEEDED, "freed 2.2 KB from 2 directories")
    assert not (artifact_tree / "target").exists() and not (artifact_tree / "web" / "dist").exists()
    assert (artifact_tree / ".venv").is_dir() and (artifact_tree / "src" / "main.py").is_file()
    assert (artifact_tree / "web" / "node_modules" / "pkg" / "index.js").is_file()
    assert await env.result(ref.job_id) == {"freed_bytes": 2300, "removed": ["target", "web/dist"]}
    remaining = [a.path for a in (await env.actions.artifact_sizes(ArtifactScope(project_id="alpha"))).artifacts]
    assert "target" not in remaining and ".venv" in remaining


async def test_clean_publishes_the_same_persisted_event_as_other_actions(env: Env, artifact_tree: Path) -> None:
    async with env.runtime.bus.subscribe() as events:
        ref = await env.actions.clean_artifacts(CleanInput(project_id="alpha", paths=[".pytest_cache"]))
        await env.jobs.wait(ref.job_id)
        published = [events.get_nowait() for _ in range(events.qsize())]

    [ran] = [event for event in published if event.type == "actions.ran"]
    assert ran.title == "Ran Clean build artifacts: freed 20 B from 1 directories" and ran.severity == "success"
    assert ran.data["action_id"] == "clean-artifacts"


async def test_clean_refuses_paths_that_artifact_sizes_did_not_report(env: Env, artifact_tree: Path, git: Git) -> None:
    refused = ["src", ".git", "src/main.py", "../alpha", "a/b/c/node_modules", "missing", "web\\dist", "./target"]
    for path in refused:
        with pytest.raises(ActionError, match="not reported by artifact sizes") as failure:
            await env.actions.clean_artifacts(CleanInput(project_id="alpha", paths=["target", path]))
        assert failure.value.status == 422, path
    assert (artifact_tree / "target").is_dir() and (artifact_tree / "src" / "main.py").is_file()
    assert (artifact_tree / ".git").is_dir()
    assert (await env.actions.history(HistoryInput(project_id="alpha"))).jobs == []


def test_clean_needs_at_least_one_path() -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        CleanInput(project_id="alpha", paths=[])


async def test_clean_removes_read_only_files(env: Env, artifact_tree: Path) -> None:
    locked = artifact_tree / "dist" / "locked.js"
    _write(locked, 3)
    locked.chmod(0o444)

    ref = await env.actions.clean_artifacts(CleanInput(project_id="alpha", paths=["dist"]))

    assert (await env.jobs.wait(ref.job_id)).status is JobStatus.SUCCEEDED
    assert not (artifact_tree / "dist").exists()


async def test_a_deletion_failure_fails_the_job_and_reports_what_was_removed(
    env: Env, artifact_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_remove = actions_module.remove_tree

    def remove_unless_target(directory: Path) -> None:
        if directory.name == "target":
            raise PermissionError(13, "Access is denied")
        real_remove(directory)

    monkeypatch.setattr(actions_module, "remove_tree", remove_unless_target)
    ref = await env.actions.clean_artifacts(CleanInput(project_id="alpha", paths=["web/dist", "target", ".venv"]))

    summary = await env.jobs.wait(ref.job_id)
    assert (summary.status, summary.summary) == (JobStatus.FAILED, "could not remove target: Access is denied")
    assert await env.result(ref.job_id) == {"freed_bytes": 300, "removed": ["web/dist"]}
    assert (artifact_tree / ".venv").is_dir() and (artifact_tree / "target").is_dir()


def test_a_reported_path_that_became_a_link_or_left_the_project_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "dist").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert artifact_target(root, "dist") == root / "dist"
    with pytest.raises(ActionError, match="lies outside the project") as outside_error:
        artifact_target(root, "../outside")
    assert outside_error.value.status == 422
    (root / "dist").rmdir()
    try:
        (root / "dist").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs privileges on this machine")
    with pytest.raises(ActionError, match="is a link now") as link_error:
        artifact_target(root, "dist")
    assert link_error.value.status == 409


def test_finding_artifacts_stops_at_the_search_depth_but_not_for_pycache(tmp_path: Path) -> None:
    for relative in ("node_modules", "a/node_modules", "a/b/node_modules", "a/b/c/node_modules", "a/b/c/d/__pycache__"):
        (tmp_path / relative).mkdir(parents=True)
    (tmp_path / ".git" / "dist").mkdir(parents=True)

    found = {
        path.relative_to(tmp_path).as_posix()
        for path in find_artifact_dirs(tmp_path, ["node_modules", "dist", "__pycache__"])
    }

    assert found == {"node_modules", "a/node_modules", "a/b/node_modules", "a/b/c/d/__pycache__"}


def test_sizes_count_files_but_no_links(tmp_path: Path) -> None:
    _write(tmp_path / "one", 10)
    _write(tmp_path / "sub" / "two", 5)
    assert directory_size(tmp_path) == 15


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**4, "3.0 TB"),
        (2048 * 1024**4, "2048.0 TB"),
    ],
)
def test_human_readable_sizes(size: int, text: str) -> None:
    assert human_bytes(size) == text
