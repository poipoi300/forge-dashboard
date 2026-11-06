"""Git parsing against real git output, and the git service on fixture repos."""

import dataclasses
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
from forge_dashboard.services.git import (
    BranchInput,
    CommitsInput,
    DiffInput,
    GitService,
    PushInput,
    RepoState,
)
from forge_dashboard.services.jobs import JobsService, JobStatus
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectRecord, ProjectsService


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


# --- remotes never carry secrets out of the dashboard ---


def test_remote_urls_lose_their_userinfo_wherever_git_reports_them() -> None:
    raw = "\n".join(
        [
            "origin\thttps://me:s3cret@github.com/o/r.git (fetch)",
            "origin\thttps://me:s3cret@github.com/o/r.git (push)",
            "token\thttps://ghp_abc123@github.com/o/r.git (fetch)",
            "pass@word\thttps://user:p@ss@host.example/o/r.git (fetch)",
            "login\tssh://git@github.com/o/r.git (fetch)",
            "scp\tgit@github.com:o/r.git (fetch)",
            "local\tC:/repos/r.git (fetch)",
        ]
    )
    assert gitparse.parse_remotes(raw) == {
        "origin": "https://github.com/o/r.git",
        "token": "https://github.com/o/r.git",
        "pass@word": "https://host.example/o/r.git",
        "login": "ssh://github.com/o/r.git",
        "scp": "git@github.com:o/r.git",
        "local": "C:/repos/r.git",
    }
    assert gitparse.redact_url("https://host.example/path@not-userinfo") == "https://host.example/path@not-userinfo"


@pytest.mark.parametrize(
    ("url", "embeds"),
    [
        ("https://me:s3cret@github.com/o/r.git", True),
        ("https://ghp_abc123@github.com/o/r.git", True),
        ("ssh://git:pw@host/o/r.git", True),
        ("ssh://git@github.com/o/r.git", False),
        ("git@github.com:o/r.git", False),
        ("https://github.com/o/r.git", False),
        ("C:/repos/r.git", False),
    ],
)
def test_only_secrets_count_as_embedded_credentials(url: str, embeds: bool) -> None:
    assert gitparse.has_embedded_credentials(url) is embeds


async def test_a_remote_with_a_token_is_published_without_it(
    git_service: tuple[GitService, ProjectsService, DiscoveryService, JobsService],
    make_project: Callable[..., Path],
    git: Git,
) -> None:
    service, projects, discovery, _jobs = git_service
    alpha = make_project("Alpha")
    git(alpha, "remote", "add", "origin", "https://me:s3cret@github.com/octocat/Hello-World.git")
    await discovery.add_project(PathInput(path=str(alpha)))

    state = await service.refresh(projects.record("alpha"))

    assert state.remotes == {"origin": "https://github.com/octocat/Hello-World.git"}
    summary = projects.summary("alpha")
    assert summary.git is not None and summary.git.github_repo == "octocat/Hello-World"
    assert summary.git.origin_url == "https://github.com/octocat/Hello-World.git"
    assert "s3cret" not in service.state.model_dump_json() + summary.model_dump_json()


# --- history, revisions, and refs ---


async def test_logging_an_unknown_revision_fails_instead_of_returning_an_empty_history(
    tmp_path: Path, git: Git
) -> None:
    repo = git.init(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git.commit_all(repo, "Add a")

    with pytest.raises(gitops.GitError, match="bad revision"):
        await gitops.log(repo, "no-such-branch")
    assert [c.subject for c in await gitops.log(repo, "HEAD")] == ["Add a"]


async def test_git_commands_on_a_missing_directory_raise_a_git_error(tmp_path: Path) -> None:
    with pytest.raises(gitops.GitError, match="is not a directory"):
        await gitops.status(tmp_path / "deleted-project")


async def test_ref_checks_treat_option_looking_text_as_a_name_not_an_option(tmp_path: Path, git: Git) -> None:
    repo = git.init(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    sha = git.commit_all(repo, "Add a")
    git(repo, "remote", "add", "origin", str(tmp_path / "elsewhere.git"))
    option = "--output=" + (tmp_path / "pwned").as_posix()

    assert not await gitops.ref_exists(repo, option)
    assert await gitops.verified_commit(repo, "HEAD") == sha
    with pytest.raises(gitops.GitError, match="not a commit"):
        await gitops.verified_commit(repo, option)
    assert await gitops.require_remote(repo, "origin") == "origin"
    for refused in ("--receive-pack=touch pwned", "missing"):
        with pytest.raises(gitops.GitError, match="not a remote"):
            await gitops.require_remote(repo, refused)
    assert await gitops.require_local_branch(repo, "main") == "main"
    with pytest.raises(gitops.GitError, match="not a local branch"):
        await gitops.require_local_branch(repo, "--force")
    assert not (tmp_path / "pwned").exists()


# --- the default branch is found, not assumed ---


async def test_the_default_branch_is_the_manifests_else_origins_head_else_the_checked_out_branch_else_main(
    tmp_path: Path, git: Git
) -> None:
    origin = git.init(tmp_path / "origin.git", branch="trunk", bare=True)
    seed = git.init(tmp_path / "seed", branch="trunk")
    (seed / "a.txt").write_text("a\n", encoding="utf-8")
    git.commit_all(seed, "Seed")
    git(seed, "push", "-q", str(origin), "trunk")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))

    assert await gitops.default_branch(clone, declared="release") == "release"
    assert await gitops.default_branch(clone, declared=None) == "trunk", "origin/HEAD names trunk"

    git(clone, "switch", "-q", "-c", "feature")
    git(clone, "remote", "set-head", "origin", "--delete")
    assert await gitops.default_branch(clone, declared=None) == "feature", "no origin/HEAD: the checked-out branch"

    git(clone, "switch", "-q", "--detach", "HEAD")
    git(clone, "branch", "-q", "-m", "trunk", "main")
    assert await gitops.default_branch(clone, declared=None) == "main", "detached with no hint: main, since it exists"

    git(clone, "branch", "-q", "-m", "main", "unrelated")
    git(clone, "branch", "-q", "-D", "feature")
    with pytest.raises(gitops.GitError, match=r"set default_branch in forge.project.toml"):
        await gitops.default_branch(clone, declared=None)


# --- when a checkout last fetched ---


async def test_fetch_time_comes_from_the_checkouts_own_fetch_head_including_linked_worktrees(
    tmp_path: Path, git: Git
) -> None:
    origin = git.init(tmp_path / "origin.git", bare=True)
    seed = git.init(tmp_path / "seed")
    (seed / "a.txt").write_text("a\n", encoding="utf-8")
    git.commit_all(seed, "Seed")
    git(seed, "push", "-q", str(origin), "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    linked = tmp_path / "linked"
    git(clone, "worktree", "add", "-q", "--detach", str(linked))
    assert (linked / ".git").is_file(), "a linked worktree's .git is a pointer file"

    assert await gitops.fetch_time(clone) is None
    assert await gitops.fetch_time(linked) is None

    git(linked, "fetch", "-q", "origin")
    assert await gitops.fetch_time(linked) is not None
    assert await gitops.fetch_time(clone) is None, "FETCH_HEAD is per worktree"
    git(clone, "fetch", "-q", "origin")
    assert await gitops.fetch_time(clone) is not None


# --- request strings are never options ---


async def test_injection_strings_in_requests_are_refused_and_run_nothing(
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
    pwned = tmp_path / "pwned.txt"
    option = f"--output={pwned.as_posix()}"
    jobs_before = len(jobs.state.jobs)

    with pytest.raises(gitops.GitError):
        await service.commits(CommitsInput(project_id="alpha", revision=option))
    with pytest.raises(gitops.GitError, match="not a commit"):
        await service.diff(DiffInput(project_id="alpha", commit=option))
    with pytest.raises(gitops.GitError, match="not a remote"):
        await service.push(PushInput(project_id="alpha", remote=f"--receive-pack=touch {pwned.as_posix()}"))
    with pytest.raises(gitops.GitError, match="not a local branch"):
        await service.push(PushInput(project_id="alpha", branch=f"--receive-pack=touch {pwned.as_posix()}"))
    for switch in (False, True):
        with pytest.raises(gitops.GitError):
            await service.create_branch(BranchInput(project_id="alpha", name=option, switch=switch))
        with pytest.raises(gitops.GitError):
            await service.create_branch(
                BranchInput(project_id="alpha", name="topic", start_point=option, switch=switch)
            )

    assert not pwned.exists()
    assert len(jobs.state.jobs) == jobs_before, "a refused push never became a job"
    assert git(alpha, "branch", "--list") == "* main"


async def test_revisions_ranges_and_branches_still_work_when_they_are_real(
    git_service: tuple[GitService, ProjectsService, DiscoveryService, JobsService],
    make_project: Callable[..., Path],
    git: Git,
) -> None:
    service, _projects, discovery, _jobs = git_service
    alpha = make_project("Alpha")
    (alpha / "more.txt").write_text("more\n", encoding="utf-8")
    git.commit_all(alpha, "More")
    await discovery.add_project(PathInput(path=str(alpha)))

    ranged = await service.commits(CommitsInput(project_id="alpha", revision="HEAD~1..HEAD"))
    assert [c.subject for c in ranged.commits] == ["More"]
    await service.create_branch(BranchInput(project_id="alpha", name="from-start", start_point="HEAD~1", switch=True))
    assert git(alpha, "branch", "--show-current") == "from-start"
    assert git(alpha, "log", "-1", "--format=%s") == "Init Alpha"


# --- the refresh loops survive a bad iteration ---


async def test_a_project_directory_that_disappeared_is_a_refresh_error_not_an_exception(
    git_service: tuple[GitService, ProjectsService, DiscoveryService, JobsService],
    make_project: Callable[..., Path],
    tmp_path: Path,
) -> None:
    service, projects, discovery, _jobs = git_service
    alpha = make_project("Alpha")
    await discovery.add_project(PathInput(path=str(alpha)))
    gone = dataclasses.replace(projects.record("alpha"), path=tmp_path / "deleted-since")

    state = await service.refresh(gone)

    assert state.error is not None and "is not a directory" in state.error


async def test_the_polling_refresh_keeps_running_after_an_iteration_fails(
    git_service: tuple[GitService, ProjectsService, DiscoveryService, JobsService],
    make_project: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _projects, discovery, _jobs = git_service
    await discovery.add_project(PathInput(path=str(make_project("Alpha"))))
    await wait_for(lambda: service.repo_state("alpha") is not None)
    original = service.refresh
    calls: list[str] = []

    async def flaky(record: ProjectRecord) -> RepoState:
        calls.append(record.id)
        if len(calls) == 1:
            raise RuntimeError("one bad iteration")
        return await original(record)

    monkeypatch.setattr(service, "refresh", flaky)
    service.config = service.config.model_copy(update={"poll_s": 0.05})  # below the config's own floor, for the test
    await service.stop()
    await service.start()

    await wait_for(lambda: len(calls) >= 3, timeout_s=10)
