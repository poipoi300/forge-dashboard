"""The agent harness: argv and transcript parsing, isolated runs, branch work, and session insight."""

import json
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from conftest import Git
from pydantic import JsonValue

from forge_dashboard import worktrees
from forge_dashboard.branchwork import BranchRunState
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, NoInput, ProjectRef
from forge_dashboard.domain import harness
from forge_dashboard.domain.harness import HarnessConfig, RunKindSettings
from forge_dashboard.services.agents import (
    AgentsConfig,
    AgentsService,
    BranchRunInput,
    BranchRunsInput,
    DelegateInput,
    IssueBrief,
    OpenPrInput,
    RunsInput,
    TranscriptInput,
    UpdateDepsInput,
)
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.git import GitService
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService
from forge_dashboard.services.scheduler import SchedulerService

FAKE_HARNESS = Path(__file__).parent / "fixtures" / "fake_harness.py"


def fake_harness_config() -> HarnessConfig:
    return HarnessConfig(command=[sys.executable, str(FAKE_HARNESS)])


def test_argv_renders_only_the_options_a_run_sets() -> None:
    config = HarnessConfig()
    minimal = harness.render_argv(config, RunKindSettings(), "hi", "/p.md")
    assert minimal == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
    ]
    settings = RunKindSettings(model="haiku", allowed_tools=["Read", "Edit"], disallowed_tools=["Bash"], max_turns=5)
    full = harness.render_argv(config, settings, "hi", "/p.md")
    assert full[-10:] == [
        "--model",
        "haiku",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read,Edit",
        "--disallowedTools",
        "Bash",
        "--max-turns",
        "5",
    ]
    templated = HarnessConfig(
        command=["pi", "--print", "{prompt}", "--file", "{prompt_path}"], stdin=None, model_args=[]
    )
    assert harness.render_argv(templated, RunKindSettings(model="x"), "a {literal}", "/p.md")[:5] == [
        "pi",
        "--print",
        "a {literal}",
        "--file",
        "/p.md",
    ]
    assert harness.render_stdin(templated, "x", "/p") is None
    assert harness.render_stdin(config, "x", "/p") == b"x"


def test_stream_json_records_become_readable_log_lines() -> None:
    assert harness.parse_record("warning: not json") is None
    assert harness.parse_record("[1, 2]") is None
    init: dict[str, JsonValue] = {"type": "system", "subtype": "init", "model": "haiku"}
    assert harness.describe_record(init) == ["agent: session started (model haiku)"]
    assistant: dict[str, JsonValue] = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Fixing it"},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
            ]
        },
    }
    assert harness.describe_record(assistant) == ["agent: Fixing it", "tool: Edit a.py"]
    denied: JsonValue = [{"type": "tool_result", "is_error": True, "content": "denied"}]
    tool_error: dict[str, JsonValue] = {"type": "user", "message": {"content": denied}}
    assert harness.describe_record(tool_error) == ["tool error: denied"]
    result: dict[str, JsonValue] = {
        "type": "result",
        "subtype": "success",
        "total_cost_usd": 0.5,
        "num_turns": 3,
        "duration_ms": 1200,
    }
    assert harness.describe_record(result) == ["result: success, $0.5000, 3 turns, 1200 ms"]
    parsed = harness.result_of(result)
    assert (parsed.cost_usd, parsed.turns, parsed.duration_ms, parsed.is_error) == (0.5, 3, 1200, False)


class FakeGitHub:
    def __init__(self) -> None:
        self.pulls: list[tuple[str, str, str, str]] = []

    async def issue_brief(self, project_id: str, number: int) -> IssueBrief:
        return IssueBrief(number=number, title="Add a greeting", body="Say hello.", url=None)

    async def open_pull_request(self, project_id: str, head: str, base: str, title: str, body: str) -> str:
        self.pulls.append((project_id, head, base, title))
        return f"https://github.com/o/r/pull/{len(self.pulls)}"


@pytest.fixture
async def agents_env(
    data_paths: DataPaths, tmp_path: Path
) -> AsyncIterator[tuple[AgentsService, ProjectsService, DiscoveryService, JobsService, FakeGitHub, Runtime]]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    scheduler = SchedulerService(runtime, jobs)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    git = GitService(runtime, projects, jobs)
    checks = ChecksService(runtime, projects, git, jobs, scheduler)
    agents = AgentsService(runtime, projects, jobs, checks)
    github = FakeGitHub()
    agents.connect_github(github, github)
    runtime.register([jobs, scheduler, manifests, projects, git, checks, agents, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "none")]})
    config = AgentsConfig(harness=fake_harness_config(), claude_projects_dir=str(tmp_path / "claude-projects"))
    await runtime.db.kv_put("config", "agents", config.model_dump(mode="json"))
    await runtime.start()
    try:
        yield agents, projects, discovery, jobs, github, runtime
    finally:
        await runtime.stop()


async def test_runs_outside_dashboard_worktrees_are_refused(
    agents_env: tuple[AgentsService, ProjectsService, DiscoveryService, JobsService, FakeGitHub, Runtime],
    tmp_path: Path,
) -> None:
    agents, _projects, _discovery, jobs, _github, _runtime = agents_env
    errors: list[str] = []

    async def work(context: JobContext) -> JobOutcome:
        try:
            await agents.run_agent(context, kind="delegate", project_id=None, prompt="x", cwd=tmp_path)
        except ActionError as error:
            errors.append(error.message)
        return JobOutcome()

    await jobs.wait((await jobs.submit(kind="t", title="t", work=work)).job_id)
    assert errors and "only in dashboard worktrees" in errors[0]


async def test_delegate_issue_lands_on_a_branch_and_merges_by_fast_forward(
    agents_env: tuple[AgentsService, ProjectsService, DiscoveryService, JobsService, FakeGitHub, Runtime],
    make_project: Callable[..., Path],
    git: Git,
) -> None:
    agents, _projects, discovery, jobs, _github, _runtime = agents_env
    alpha = make_project("Alpha")
    await discovery.add_project(PathInput(path=str(alpha)))
    ref = await agents.delegate_issue(DelegateInput(project_id="alpha", issue_number=7, instructions="Keep it short"))
    final = await jobs.wait(ref.job_id)
    assert final.status is JobStatus.ATTENTION and final.summary is not None and "1 files changed" in final.summary
    [run] = (await agents.branch_runs(BranchRunsInput(project_id="alpha"))).runs
    assert run.state is BranchRunState.READY and run.branch.startswith("forge-issue/7/")
    assert not (alpha / "AGENT_WAS_HERE.md").exists(), "the user's working tree must not change"
    diff = await agents.branch_diff(BranchRunInput(run_id=run.id))
    assert "AGENT_WAS_HERE.md" in diff.diff and "Implement GitHub issue #7" in diff.diff

    [agent_run] = (await agents.runs(RunsInput(project_id="alpha"))).runs
    assert (agent_run.outcome, agent_run.cost_usd, agent_run.turns) == ("succeeded", 0.0123, 1)
    transcript = await agents.transcript(TranscriptInput(run_id=agent_run.id))
    assert transcript.done and transcript.records[0]["type"] == "system"
    assert transcript.prompt is not None and "Keep it short" in transcript.prompt

    merged = await agents.merge_branch(BranchRunInput(run_id=run.id))
    assert merged.state is BranchRunState.MERGED
    assert (alpha / "AGENT_WAS_HERE.md").exists(), "a clean checkout follows the fast-forward"
    assert git(alpha, "branch", "--list", "forge-issue/*") == ""
    with pytest.raises(ActionError, match="not ready"):
        await agents.merge_branch(BranchRunInput(run_id=run.id))


async def test_dirty_checkouts_keep_the_branch_and_discard_removes_it(
    agents_env: tuple[AgentsService, ProjectsService, DiscoveryService, JobsService, FakeGitHub, Runtime],
    make_project: Callable[..., Path],
    git: Git,
) -> None:
    agents, _projects, discovery, jobs, _github, _runtime = agents_env
    alpha = make_project("Alpha")
    await discovery.add_project(PathInput(path=str(alpha)))
    await jobs.wait((await agents.update_dependencies(UpdateDepsInput(project_id="alpha"))).job_id)
    [run] = (await agents.branch_runs(BranchRunsInput(project_id="alpha"))).runs
    (alpha / "README.md").write_text("# local edit\n", encoding="utf-8")
    with pytest.raises(ActionError, match="local changes"):
        await agents.merge_branch(BranchRunInput(run_id=run.id))
    assert (alpha / "README.md").read_text(encoding="utf-8") == "# local edit\n"
    discarded = await agents.discard_branch(BranchRunInput(run_id=run.id))
    assert discarded.state is BranchRunState.DISCARDED
    assert not Path(run.worktree).exists() and git(alpha, "branch", "--list", "forge-deps/*") == ""


async def test_open_pr_pushes_the_branch_and_records_the_url(
    agents_env: tuple[AgentsService, ProjectsService, DiscoveryService, JobsService, FakeGitHub, Runtime],
    make_project: Callable[..., Path],
    git: Git,
    tmp_path: Path,
) -> None:
    agents, _projects, discovery, jobs, github, _runtime = agents_env
    alpha = make_project("Alpha")
    remote = git.init(tmp_path / "remote.git", bare=True)
    git(alpha, "remote", "add", "origin", str(remote))
    await discovery.add_project(PathInput(path=str(alpha)))
    await jobs.wait((await agents.delegate_issue(DelegateInput(project_id="alpha", issue_number=3))).job_id)
    [run] = (await agents.branch_runs(BranchRunsInput(project_id="alpha"))).runs
    opened = await agents.open_pr(OpenPrInput(run_id=run.id))
    assert opened.state is BranchRunState.PR_OPENED and opened.pr_url == "https://github.com/o/r/pull/1"
    assert git(remote, "branch", "--list", run.branch).strip() == run.branch
    assert github.pulls == [("alpha", run.branch, "main", "Issue #3: Add a greeting")]


async def test_failed_and_missing_harness_runs_are_recorded(
    agents_env: tuple[AgentsService, ProjectsService, DiscoveryService, JobsService, FakeGitHub, Runtime],
    make_project: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents, _projects, discovery, jobs, _github, _runtime = agents_env
    await discovery.add_project(PathInput(path=str(make_project("Alpha"))))
    monkeypatch.setenv("FAKE_HARNESS_EXIT", "3")
    failed = await jobs.wait((await agents.update_dependencies(UpdateDepsInput(project_id="alpha"))).job_id)
    assert failed.status is JobStatus.FAILED and failed.summary == "agent failed"
    monkeypatch.delenv("FAKE_HARNESS_EXIT")
    await agents.update_config(AgentsConfig(harness=HarnessConfig(command=["definitely-not-a-harness-xyz"])))
    assert not (await agents.harness_status(NoInput())).available
    missing = await jobs.wait((await agents.update_dependencies(UpdateDepsInput(project_id="alpha"))).job_id)
    assert missing.summary == "agent harness-missing"
    outcomes = [run.outcome for run in (await agents.runs(RunsInput(project_id="alpha"))).runs]
    assert outcomes == ["harness-missing", "failed"]


async def test_claude_sessions_and_agent_facts(
    agents_env: tuple[AgentsService, ProjectsService, DiscoveryService, JobsService, FakeGitHub, Runtime],
    make_project: Callable[..., Path],
    tmp_path: Path,
) -> None:
    agents, projects, discovery, _jobs, _github, _runtime = agents_env
    alpha = make_project("Alpha")
    from forge_dashboard.domain.claude_sessions import encode_project_path

    session_dir = tmp_path / "claude-projects" / encode_project_path(alpha.resolve())
    session_dir.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "timestamp": "2026-11-01T10:00:00.000Z",
            "cwd": str(alpha.resolve()),
            "sessionId": "s1",
            "message": {"role": "user", "content": "Build the thing\nwith details"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-01T10:05:00.000Z",
            "cwd": str(alpha.resolve()),
            "sessionId": "s1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        },
    ]
    (session_dir / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    await discovery.add_project(PathInput(path=str(alpha)))
    found = await agents.sessions(ProjectRef(project_id="alpha"))
    assert [(s.id, s.first_prompt, s.duration_s) for s in found.sessions] == [("s1", "Build the thing", 300.0)]
    facts = projects.summary("alpha").agent
    assert facts.session_count == 1 and facts.last_session_at is not None and not facts.active


async def test_worktree_commits_use_the_repo_identity_or_the_dashboards(tmp_path: Path, git: Git) -> None:
    repo = git.init(tmp_path / "repo")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    assert await worktrees.identity_env(repo) != {} or git(repo, "config", "user.email", check=False)
    git(repo, "config", "user.email", "me@example.com")
    git(repo, "config", "user.name", "Me")
    assert await worktrees.identity_env(repo) == {}
    sha = await worktrees.commit_all(repo, "Add a")
    assert sha is not None and git(repo, "log", "-1", "--format=%ae") == "me@example.com"
    assert await worktrees.commit_all(repo, "Nothing") is None
