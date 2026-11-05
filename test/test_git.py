"""Git parsing against real git output, and the git service on fixture repos."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from conftest import Git
from testing_support import wait_for

from forge_dashboard import gitops
from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, ProjectRef
from forge_dashboard.domain import gitparse
from forge_dashboard.services.discovery import DiscoveryService, PathInput
from forge_dashboard.services.git import BranchInput, CommitsInput, DiffInput, GitService, PushInput
from forge_dashboard.services.jobs import JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService


def test_status_v2_parses_branch_headers_and_every_entry_kind() -> None:
    raw = "\x00".join(
        [
            "# branch.oid 1234567890abcdef",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -1",
            "1 .M N... 100644 100644 100644 aaa bbb src/app.py",
            "1 A. N... 000000 100644 100644 000 ccc new.py",
            "2 R. N... 100644 100644 100644 ddd eee R100 renamed.py",
            "old.py",
            "u UU N... 100644 100644 100644 100644 f1 f2 f3 conflict.txt",
            "? notes with spaces.txt",
            "",
        ]
    )
    status = gitparse.parse_status_v2(raw)
    assert status.branch == gitparse.BranchStatus("main", "1234567890abcdef", "origin/main", 2, 1)
    kinds = [(entry.path, entry.kind) for entry in status.entries]
    assert kinds == [
        ("src/app.py", "modified"),
        ("new.py", "added"),
        ("renamed.py", "renamed"),
        ("conflict.txt", "conflicted"),
        ("notes with spaces.txt", "untracked"),
    ]
    assert status.entries[2].original_path == "old.py"
    assert [entry.path for entry in status.conflicted] == ["conflict.txt"]


def test_status_v2_handles_initial_and_detached_heads() -> None:
    status = gitparse.parse_status_v2("# branch.oid (initial)\x00# branch.head (detached)\x00")
    assert status.branch.oid is None and status.branch.head is None and status.dirty == 0


def test_merge_tree_output_lists_conflicted_files_once() -> None:
    clean = gitparse.parse_merge_tree("abc123\n", 0)
    assert clean.clean and clean.tree == "abc123"
    conflicted = gitparse.parse_merge_tree("abc123\nshared.txt\nshared.txt\nother.py\n\nAuto-merging shared.txt\n", 1)
    assert not conflicted.clean
    assert conflicted.conflicted_files == ("shared.txt", "other.py")
    assert conflicted.messages == ("Auto-merging shared.txt",)


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/octocat/Hello-World.git", "octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World", "octocat/Hello-World"),
        ("https://user@github.com/o/r.git", "o/r"),
        ("git@github.com:o/r.git", "o/r"),
        ("ssh://git@github.com/o/r.git", "o/r"),
        ("https://gitlab.com/o/r.git", None),
        ("C:/repos/upstream.git", None),
    ],
)
def test_github_slugs_are_parsed_from_every_remote_form(url: str, slug: str | None) -> None:
    assert gitparse.github_slug(url) == slug


def test_branch_tracking_states() -> None:
    sep = gitparse.FIELD
    raw = "\n".join(
        [
            f"*{sep}main{sep}origin/main{sep}ahead 2, behind 1{sep}100{sep}abc{sep}Work",
            f" {sep}old{sep}origin/old{sep}gone{sep}50{sep}def{sep}Old",
            f" {sep}synced{sep}origin/synced{sep}{sep}60{sep}aaa{sep}Same",
            f" {sep}local{sep}{sep}{sep}70{sep}bbb{sep}Local",
        ]
    )
    main, old, synced, local = gitparse.parse_branches(raw)
    assert (main.is_head, main.ahead, main.behind) == (True, 2, 1)
    assert old.gone and old.ahead is None
    assert (synced.ahead, synced.behind) == (0, 0)
    assert (local.upstream, local.ahead) == (None, None)


def test_worktree_porcelain_blocks() -> None:
    raw = "worktree C:/repo\nHEAD abc\nbranch refs/heads/main\n\nworktree C:/wt\nHEAD def\ndetached\nlocked\n"
    main, other = gitparse.parse_worktrees(raw)
    assert (main.branch, main.detached) == ("main", False)
    assert (other.branch, other.detached, other.locked) == (None, True, True)


async def test_real_git_log_and_status_round_trip(tmp_path: Path, git: Git) -> None:
    repo = git.init(tmp_path / "repo")
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    git.commit_all(repo, "Add a")
    (repo / "a.txt").write_text("one\nthree\nfour\n", encoding="utf-8")
    git.commit_all(repo, "Change a")
    (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    commits = await gitops.log(repo, limit=5)
    assert [c.subject for c in commits] == ["Change a", "Add a"]
    assert (commits[0].files_changed, commits[0].insertions, commits[0].deletions) == (1, 2, 1)
    status = await gitops.status(repo)
    assert status.branch.head == "main" and status.dirty == 2
    assert await gitops.newest_change_mtime(repo, ["a.txt", "b.txt", "missing.txt"]) is not None
    empty = git.init(tmp_path / "empty")
    assert await gitops.log(empty) == []
    with pytest.raises(gitops.GitError, match="git rev-list failed"):
        await gitops.count(repo, "nope..HEAD")


async def test_merge_tree_predicts_conflicts_without_a_checkout(tmp_path: Path, git: Git) -> None:
    repo = git.init(tmp_path / "repo")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    git.commit_all(repo, "Base")
    git(repo, "switch", "-q", "-c", "theirs")
    (repo / "shared.txt").write_text("theirs\n", encoding="utf-8")
    git.commit_all(repo, "Theirs")
    git(repo, "switch", "-q", "main")
    (repo / "shared.txt").write_text("ours\n", encoding="utf-8")
    git.commit_all(repo, "Ours")
    prediction = await gitops.merge_tree(repo, "main", "theirs")
    assert not prediction.clean and prediction.conflicted_files == ("shared.txt",)
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "ours\n"


@pytest.fixture
async def git_service(
    data_paths: DataPaths, tmp_path: Path
) -> AsyncIterator[tuple[GitService, ProjectsService, DiscoveryService, JobsService]]:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    service = GitService(runtime, projects, jobs)
    runtime.register([jobs, manifests, projects, service, discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path / "nothing-here")]})
    await runtime.start()
    try:
        yield service, projects, discovery, jobs
    finally:
        await runtime.stop()


async def test_git_facts_reach_the_summary(
    git_service: tuple[GitService, ProjectsService, DiscoveryService, JobsService],
    make_project: Callable[..., Path],
    git: Git,
    tmp_path: Path,
) -> None:
    service, projects, discovery, _jobs = git_service
    alpha = make_project("Alpha")
    remote = git.init(tmp_path / "remote.git", bare=True)
    git(alpha, "remote", "add", "origin", str(remote))
    git(alpha, "push", "-q", "-u", "origin", "main")
    (alpha / "feature.py").write_text("x = 1\n", encoding="utf-8")
    git.commit_all(alpha, "Add feature")
    git(alpha, "stash", "list")
    (alpha / "scratch.txt").write_text("wip\n", encoding="utf-8")
    await discovery.add_project(PathInput(path=str(alpha)))
    record = projects.record("alpha")
    await service.refresh(record)
    summary = projects.summary("alpha")
    assert summary.git is not None
    assert (summary.git.branch, summary.git.dirty, summary.git.ahead, summary.git.unpushed) == ("main", 1, 1, 1)
    assert summary.last_change.source == "working-tree"
    assert summary.git.github_repo is None and not summary.git.has_upstream_remote

    changes = await service.changes(ProjectRef(project_id="alpha"))
    assert [(f.path, f.kind) for f in changes.files] == [("scratch.txt", "untracked")]
    untracked_diff = await service.diff(DiffInput(project_id="alpha", path="scratch.txt"))
    assert "+wip" in untracked_diff.diff
    commits = await service.commits(CommitsInput(project_id="alpha", limit=5))
    assert commits.commits[0].subject == "Add feature"
    commit_diff = await service.diff(DiffInput(project_id="alpha", commit=commits.commits[0].sha))
    assert "+x = 1" in commit_diff.diff
    branches = await service.branches(ProjectRef(project_id="alpha"))
    assert branches.branches[0].ahead == 1 and branches.branches[0].current
    assert (await service.worktrees(ProjectRef(project_id="alpha"))).worktrees[0].dashboard_owned is False
    assert (await service.stashes(ProjectRef(project_id="alpha"))).stashes == []
    git(alpha, "tag", "v0.1", "HEAD~1")
    tags = await service.tags(ProjectRef(project_id="alpha"))
    assert [t.name for t in tags.tags] == ["v0.1"] and tags.commits_since_latest == 1


async def test_explicit_writes_run_as_jobs(
    git_service: tuple[GitService, ProjectsService, DiscoveryService, JobsService],
    make_project: Callable[..., Path],
    git: Git,
    tmp_path: Path,
) -> None:
    service, _projects, discovery, jobs = git_service
    alpha = make_project("Alpha")
    remote = git.init(tmp_path / "remote.git", bare=True)
    git(alpha, "remote", "add", "origin", str(remote))
    await discovery.add_project(PathInput(path=str(alpha)))
    pushed = await jobs.wait((await service.push(PushInput(project_id="alpha"))).job_id)
    assert pushed.status is JobStatus.SUCCEEDED
    assert git(remote, "rev-parse", "main") == git(alpha, "rev-parse", "HEAD")
    fetched = await jobs.wait((await service.fetch(ProjectRef(project_id="alpha"))).job_id)
    assert fetched.status is JobStatus.SUCCEEDED
    pulled = await jobs.wait((await service.pull(ProjectRef(project_id="alpha"))).job_id)
    assert pulled.status is JobStatus.SUCCEEDED
    await service.create_branch(BranchInput(project_id="alpha", name="topic"))
    assert "topic" in git(alpha, "branch", "--list", "topic")
    assert git(alpha, "branch", "--show-current") == "main"
    await service.create_branch(BranchInput(project_id="alpha", name="other", switch=True))
    assert git(alpha, "branch", "--show-current") == "other"
    with pytest.raises(gitops.GitError, match="already exists"):
        await service.create_branch(BranchInput(project_id="alpha", name="topic"))
    state = service.repo_state("alpha")
    assert state is not None and state.branch == "other"


async def test_watcher_changes_refresh_after_the_debounce(
    git_service: tuple[GitService, ProjectsService, DiscoveryService, JobsService], make_project: Callable[..., Path]
) -> None:
    service, projects, discovery, _jobs = git_service
    alpha = make_project("Alpha")
    await discovery.add_project(PathInput(path=str(alpha)))
    await wait_for(lambda: service.repo_state("alpha") is not None)
    (alpha / "new.txt").write_text("x", encoding="utf-8")
    await service.files_changed(projects.record("alpha"), frozenset({"worktree"}))
    await wait_for(lambda: (state := service.repo_state("alpha")) is not None and state.dirty == 1, timeout_s=5)


async def test_non_git_projects_are_refused(
    git_service: tuple[GitService, ProjectsService, DiscoveryService, JobsService], tmp_path: Path
) -> None:
    service, _projects, discovery, _jobs = git_service
    plain = tmp_path / "plain"
    plain.mkdir()
    await discovery.add_project(PathInput(path=str(plain)))
    with pytest.raises(ActionError, match="not a git repository"):
        await service.changes(ProjectRef(project_id="plain"))
