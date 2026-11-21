"""Git state per project, git browsing queries, and explicit git actions.

State refreshes when the watcher reports git or working-tree changes
(debounced), and on a polling interval as a fallback. Reads never take locks
or write to the user's repository. Writes (fetch, pull, push, create branch)
happen only on an explicit click and run as jobs.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, override

from pydantic import BaseModel, Field

from forge_dashboard import gitops
from forge_dashboard.core.events import Event
from forge_dashboard.core.service import ActionError, Done, JobRef, ProjectRef, Service, action
from forge_dashboard.core.tasks import BackgroundTasks, repeat
from forge_dashboard.core.ui import ui
from forge_dashboard.domain import gitparse
from forge_dashboard.domain.summary import GitFacts
from forge_dashboard.github.remote import parse_github_slug
from forge_dashboard.services.discovery import ChangeKind
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

BACKFILL_COMMITS = 20


class RepoState(BaseModel):
    project_id: str
    branch: str | None = Field(None, json_schema_extra=ui(format="code"))
    head: str | None = Field(None, json_schema_extra=ui(format="code"))
    upstream: str | None = None
    dirty: int = 0
    ahead: int | None = None
    behind: int | None = None
    unpushed: int = 0
    stashes: int = 0
    last_commit_subject: str | None = None
    last_commit_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    last_worktree_change_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    remotes: dict[str, str] = Field(default_factory=dict)
    refreshed_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    error: str | None = None


class GitConfig(BaseModel):
    poll_s: int = Field(60, ge=10, le=3600, json_schema_extra=ui(widget="duration", help="Fallback refresh period."))
    debounce_ms: int = Field(
        500, ge=50, le=10_000, json_schema_extra=ui(advanced=True, unit="ms", help="Wait after a file change.")
    )


class GitState(BaseModel):
    repos: list[RepoState] = Field(
        default_factory=list, json_schema_extra=ui(columns=["project_id", "branch", "dirty", "ahead", "behind"])
    )


class FileChange(BaseModel):
    path: str = Field(json_schema_extra=ui(format="path"))
    kind: gitparse.ChangeKind = Field(
        json_schema_extra=ui(
            format="badge",
            tones={
                "modified": "info",
                "added": "good",
                "deleted": "bad",
                "renamed": "info",
                "untracked": "neutral",
                "conflicted": "bad",
                "copied": "info",
                "type-changed": "info",
            },
        )
    )
    staged: bool
    original_path: str | None = None


class Changes(BaseModel):
    branch: str | None
    files: list[FileChange] = Field(json_schema_extra=ui(columns=["kind", "path", "staged"]))


class CommitInfo(BaseModel):
    sha: str = Field(json_schema_extra=ui(hidden=True))
    short_sha: str = Field(json_schema_extra=ui(format="code"))
    author: str
    committed_at: float = Field(json_schema_extra=ui(format="relative-time"))
    subject: str
    files_changed: int
    insertions: int
    deletions: int


class CommitsInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    revision: str | None = Field(None, json_schema_extra=ui(placeholder="HEAD", help="Branch, tag, or range."))
    limit: int = Field(30, ge=1, le=500)


class Commits(BaseModel):
    commits: list[CommitInfo] = Field(json_schema_extra=ui(columns=["short_sha", "subject", "author", "committed_at"]))


class BranchInfo(BaseModel):
    name: str
    current: bool
    upstream: str | None
    ahead: int | None
    behind: int | None
    gone: bool
    committed_at: float = Field(json_schema_extra=ui(format="relative-time"))
    subject: str


class Branches(BaseModel):
    branches: list[BranchInfo] = Field(
        json_schema_extra=ui(columns=["name", "upstream", "ahead", "behind", "committed_at"])
    )


class DiffInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    path: str | None = Field(None, json_schema_extra=ui(help="One file; empty for the whole tree."))
    staged: bool = False
    commit: str | None = Field(None, json_schema_extra=ui(help="Show this commit's diff instead of the working tree."))


class Diff(BaseModel):
    diff: str = Field(json_schema_extra=ui(format="code"))


class WorktreeInfo(BaseModel):
    path: str = Field(json_schema_extra=ui(format="path"))
    branch: str | None
    head: str | None = Field(None, json_schema_extra=ui(format="code"))
    detached: bool
    locked: bool
    prunable: bool
    dashboard_owned: bool = Field(json_schema_extra=ui(help="Created by the dashboard for a sync or bring-up."))


class Worktrees(BaseModel):
    worktrees: list[WorktreeInfo] = Field(json_schema_extra=ui(columns=["path", "branch", "dashboard_owned"]))


class StashInfo(BaseModel):
    name: str
    created_at: float = Field(json_schema_extra=ui(format="relative-time"))
    message: str


class Stashes(BaseModel):
    stashes: list[StashInfo] = Field(json_schema_extra=ui(columns=["name", "message", "created_at"]))


class TagInfo(BaseModel):
    name: str
    created_at: float = Field(json_schema_extra=ui(format="relative-time"))
    short_sha: str = Field(json_schema_extra=ui(format="code"))


class Tags(BaseModel):
    tags: list[TagInfo] = Field(json_schema_extra=ui(columns=["name", "short_sha", "created_at"]))
    commits_since_latest: int | None = Field(None, json_schema_extra=ui(help="Commits on HEAD since the newest tag."))


class BranchInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    name: str = Field(json_schema_extra=ui(placeholder="feature/name"))
    start_point: str | None = Field(None, json_schema_extra=ui(placeholder="HEAD"))
    switch: bool = Field(False, json_schema_extra=ui(help="Switch the working tree to the new branch."))


class PushInput(BaseModel):
    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))
    remote: str = "origin"
    branch: str | None = Field(None, json_schema_extra=ui(help="Defaults to the current branch."))


class CommitObserved(Event):
    type: ClassVar[str] = "git.commit"
    persist: ClassVar[bool] = True
    sha: str
    subject: str
    author: str
    committed_at: float

    def title(self) -> str:
        return f"Commit {self.sha[:7]}: {self.subject}"

    def dedupe_key(self) -> str:
        return f"commit:{self.project_id}:{self.sha}"

    def occurred_at(self) -> float:
        return self.committed_at


GitWrite = Literal["fetch", "pull", "push"]


class GitService(Service[GitConfig, GitState], ProjectListener):
    id = "git"
    title = "Git"
    description = "Branch, changes, sync with upstream, and history per project; fetch, pull, and push on request."
    Config = GitConfig
    State = GitState
    events = (CommitObserved,)

    def __init__(self, runtime: Runtime, projects: ProjectsService, jobs: JobsService) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._jobs = jobs
        self._repos: dict[str, RepoState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._seen_heads: dict[str, str | None] = {}
        self._background = BackgroundTasks()
        self._poll_task: asyncio.Task[None] | None = None
        self._refresh_listeners: list[Callable[[str, RepoState], None]] = []
        projects.listen(self)

    def on_refreshed(self, listener: Callable[[str, RepoState], None]) -> None:
        """Call `listener(project_id, state)` after every refresh (staleness depends on HEAD)."""
        self._refresh_listeners.append(listener)

    # --- lifecycle ---

    @override
    async def start(self) -> None:
        self._poll_task = asyncio.create_task(
            repeat(self._refresh_all, lambda: self.config.poll_s, label="git poll refresh"), name="git-poll"
        )

    @override
    async def stop(self) -> None:
        for handle in self._pending.values():
            handle.cancel()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        await self._background.cancel_all()

    async def _refresh_all(self) -> None:
        for record in self._projects.records():
            if record.is_git:
                await self.refresh(record)

    # --- registry hooks ---

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        if record.is_git:
            self._background.spawn(self.refresh(record), name=f"git-refresh:{record.id}")

    @override
    async def project_removed(self, project_id: str) -> None:
        self._repos.pop(project_id, None)
        self._publish_state()

    @override
    async def files_changed(self, record: ProjectRecord, kinds: frozenset[ChangeKind]) -> None:
        if not record.is_git or not kinds & {"git", "worktree"}:
            return
        pending = self._pending.pop(record.id, None)
        if pending is not None:
            pending.cancel()
        loop = asyncio.get_running_loop()
        self._pending[record.id] = loop.call_later(
            self.config.debounce_ms / 1000,
            lambda: self._background.spawn(self.refresh(record), name=f"git-refresh:{record.id}"),
        )

    # --- refresh ---

    async def refresh(self, record: ProjectRecord) -> RepoState:
        """Recompute one project's git state and publish its facts."""
        lock = self._locks.setdefault(record.id, asyncio.Lock())
        async with lock:
            try:
                state = await self._read(record)
            except gitops.GitError as error:
                state = RepoState(project_id=record.id, refreshed_at=time.time(), error=error.message)
            self._repos[record.id] = state
            self._publish_state()
            self._projects.put(record.id, self._facts(state))
            for listener in self._refresh_listeners:
                listener(record.id, state)
            return state

    async def _read(self, record: ProjectRecord) -> RepoState:
        repo = record.path
        status, remotes, stashes = await asyncio.gather(
            gitops.status(repo), gitops.remotes(repo), gitops.stash_count(repo)
        )
        recent = await gitops.log(repo, limit=1, stats=False)
        last = recent[0] if recent else None
        changed_paths = [entry.path for entry in status.entries if entry.kind != "deleted"]
        worktree_change = await gitops.newest_change_mtime(repo, changed_paths) if changed_paths else None
        unpushed = 0
        if status.branch.upstream is not None:
            unpushed = status.branch.ahead or 0
        elif remotes and status.branch.oid is not None:
            unpushed = await self._unpushed(repo)
        await self._observe_commits(record, status.branch.oid)
        return RepoState(
            project_id=record.id,
            branch=status.branch.head,
            head=status.branch.oid,
            upstream=status.branch.upstream,
            dirty=status.dirty,
            ahead=status.branch.ahead,
            behind=status.branch.behind,
            unpushed=unpushed,
            stashes=stashes,
            last_commit_subject=last.subject if last is not None else None,
            last_commit_at=last.committed_at if last is not None else None,
            last_worktree_change_at=worktree_change,
            remotes=remotes,
            refreshed_at=time.time(),
        )

    async def _unpushed(self, repo: Path) -> int:
        """Commits on HEAD that no remote-tracking branch contains (for a branch without an upstream)."""
        raw = await gitops.output(repo, "rev-list", "--count", "HEAD", "--not", "--remotes")
        return int(raw.strip() or 0)

    async def _observe_commits(self, record: ProjectRecord, head: str | None) -> None:
        """Publish new commits as activity; the first sight of a repo backfills recent history."""
        previous = self._seen_heads.get(record.id, "unseen")
        self._seen_heads[record.id] = head
        if head is None or previous == head:
            return
        if previous in ("unseen", None):
            commits = await gitops.log(record.path, limit=BACKFILL_COMMITS, stats=False)
        elif await gitops.ref_exists(record.path, str(previous)):
            commits = await gitops.log(record.path, f"{previous}..{head}", limit=200, stats=False)
        else:
            commits = await gitops.log(record.path, limit=BACKFILL_COMMITS, stats=False)
        for commit in reversed(commits):
            self.publish(
                CommitObserved(
                    project_id=record.id,
                    sha=commit.sha,
                    subject=commit.subject,
                    author=commit.author,
                    committed_at=commit.committed_at,
                )
            )

    @staticmethod
    def _facts(state: RepoState) -> GitFacts:
        origin = state.remotes.get("origin")
        return GitFacts(
            branch=state.branch,
            head_sha=state.head,
            dirty=state.dirty,
            ahead=state.ahead,
            behind=state.behind,
            unpushed=state.unpushed,
            stashes=state.stashes,
            last_commit_at=state.last_commit_at,
            last_worktree_change_at=state.last_worktree_change_at,
            origin_url=origin,
            github_repo=parse_github_slug(origin) if origin else None,
            has_upstream_remote="upstream" in state.remotes,
        )

    def repo_state(self, project_id: str) -> RepoState | None:
        return self._repos.get(project_id)

    def _publish_state(self) -> None:
        self.set_state(GitState(repos=sorted(self._repos.values(), key=lambda repo: repo.project_id)))

    def _git_record(self, project_id: str) -> ProjectRecord:
        record = self._projects.record(project_id)
        if not record.is_git:
            raise ActionError(f"{record.path} is not a git repository", status=409)
        return record

    # --- queries ---

    @action(title="Working-tree changes", query=True)
    async def changes(self, request: ProjectRef) -> Changes:
        """Changed, staged, untracked, and conflicted files."""
        record = self._git_record(request.project_id)
        status = await gitops.status(record.path)
        files = [
            FileChange(
                path=entry.path,
                kind=entry.kind,
                staged=entry.index not in (".", "?"),
                original_path=entry.original_path,
            )
            for entry in status.entries
        ]
        return Changes(branch=status.branch.head, files=files)

    @action(title="Diff", query=True)
    async def diff(self, request: DiffInput) -> Diff:
        """A unified diff of the working tree, the index, one commit, or one file."""
        record = self._git_record(request.project_id)
        if request.commit is not None:
            commit = await gitops.verified_commit(record.path, request.commit)
            return Diff(diff=await gitops.diff(record.path, f"{commit}^!", "--"))
        if request.path is not None:
            status = await gitops.status(record.path)
            untracked = any(entry.path == request.path and entry.kind == "untracked" for entry in status.entries)
            if untracked:
                return Diff(diff=await gitops.diff(record.path, "--no-index", "--", "/dev/null", request.path))
            args = ["--cached"] if request.staged else []
            return Diff(diff=await gitops.diff(record.path, *args, "--", request.path))
        return Diff(diff=await gitops.diff(record.path, *(["--cached"] if request.staged else ["HEAD"])))

    @action(title="Recent commits", query=True)
    async def commits(self, request: CommitsInput) -> Commits:
        """History with diffstat."""
        record = self._git_record(request.project_id)
        revisions = [request.revision] if request.revision else []
        found = await gitops.log(record.path, *revisions, limit=request.limit)
        return Commits(
            commits=[
                CommitInfo(
                    sha=c.sha,
                    short_sha=c.short_sha,
                    author=c.author,
                    committed_at=c.committed_at,
                    subject=c.subject,
                    files_changed=c.files_changed,
                    insertions=c.insertions,
                    deletions=c.deletions,
                )
                for c in found
            ]
        )

    @action(title="Branches", query=True)
    async def branches(self, request: ProjectRef) -> Branches:
        """Local branches with their upstream and ahead/behind counts."""
        record = self._git_record(request.project_id)
        return Branches(
            branches=[
                BranchInfo(
                    name=b.name,
                    current=b.is_head,
                    upstream=b.upstream,
                    ahead=b.ahead,
                    behind=b.behind,
                    gone=b.gone,
                    committed_at=b.committed_at,
                    subject=b.subject,
                )
                for b in await gitops.branches(record.path)
            ]
        )

    @action(title="Worktrees", query=True)
    async def worktrees(self, request: ProjectRef) -> Worktrees:
        """Every worktree of the repository, marking the ones the dashboard created."""
        record = self._git_record(request.project_id)
        owned_root = self.runtime.paths.worktrees.resolve()
        return Worktrees(
            worktrees=[
                WorktreeInfo(
                    path=w.path,
                    branch=w.branch,
                    head=w.head,
                    detached=w.detached,
                    locked=w.locked,
                    prunable=w.prunable,
                    dashboard_owned=owned_root in Path(w.path).resolve().parents,
                )
                for w in await gitops.worktrees(record.path)
            ]
        )

    @action(title="Stashes", query=True)
    async def stashes(self, request: ProjectRef) -> Stashes:
        """Stash entries (read only; the dashboard never stashes your work)."""
        record = self._git_record(request.project_id)
        raw = await gitops.output(record.path, "stash", "list", "--format=%gd%x00%ct%x00%s")
        entries = [line.split(gitparse.FIELD) for line in raw.splitlines() if line]
        return Stashes(stashes=[StashInfo(name=n, created_at=float(t), message=m) for n, t, m in entries])

    @action(title="Tags", query=True)
    async def tags(self, request: ProjectRef) -> Tags:
        """Tags, newest first, and how many commits HEAD has since the newest."""
        record = self._git_record(request.project_id)
        raw = await gitops.output(
            record.path,
            "for-each-ref",
            "refs/tags",
            "--sort=-creatordate",
            "--format=%(refname:short)%00%(creatordate:unix)%00%(objectname:short)",
        )
        entries = [line.split(gitparse.FIELD) for line in raw.splitlines() if line]
        tags = [TagInfo(name=n, created_at=float(t or 0), short_sha=s) for n, t, s in entries]
        since = await gitops.count(record.path, f"{tags[0].name}..HEAD") if tags else None
        return Tags(tags=tags, commits_since_latest=since)

    # --- explicit writes ---

    async def _write_job(self, record: ProjectRecord, kind: GitWrite, title: str, argv: list[str]) -> JobRef:
        async def work(context: JobContext) -> JobOutcome:
            result = await context.run(
                ["git", "-C", str(record.path), *gitops.CONFIG, *argv],
                record.path,
                gitops.transport_env(),
                timeout_s=600,
            )
            await self.refresh(record)
            if not result.ok:
                return JobOutcome(status=JobStatus.FAILED, summary=f"git {kind} exited {result.exit_code}")
            return JobOutcome(summary=f"git {kind} done")

        return await self._jobs.submit(
            kind=f"git.{kind}", title=title, work=work, project_id=record.id, resources=[f"repo:{record.id}"]
        )

    @action(title="Fetch", long_running=True)
    async def fetch(self, request: ProjectRef) -> JobRef:
        """Fetch every remote (prunes deleted remote branches)."""
        record = self._git_record(request.project_id)
        return await self._write_job(record, "fetch", "git fetch --all", ["fetch", "--all", "--prune"])

    @action(title="Pull (fast-forward only)", long_running=True, confirm=True)
    async def pull(self, request: ProjectRef) -> JobRef:
        """Fast-forward the current branch to its upstream; never merges or rebases."""
        record = self._git_record(request.project_id)
        return await self._write_job(record, "pull", "git pull --ff-only", ["pull", "--ff-only"])

    @action(title="Push", long_running=True, confirm=True)
    async def push(self, request: PushInput) -> JobRef:
        """Push a branch; a branch without an upstream gets one on this remote."""
        record = self._git_record(request.project_id)
        current = (await gitops.status(record.path)).branch
        branch = request.branch or current.head
        if branch is None:
            raise ActionError("detached HEAD: name the branch to push", status=409)
        remote = await gitops.require_remote(record.path, request.remote)
        await gitops.require_local_branch(record.path, branch)
        upstream = ["-u"] if branch == current.head and current.upstream is None else []
        return await self._write_job(
            record, "push", f"git push {remote} {branch}", ["push", *upstream, "--", remote, branch]
        )

    @action(title="Create branch")
    async def create_branch(self, request: BranchInput) -> Done:
        """Create a branch (optionally switching to it)."""
        record = self._git_record(request.project_id)
        # The name and start point come from the request; git must never read either as an option.
        if request.switch:
            switch_start = ["--end-of-options", request.start_point] if request.start_point else []
            await gitops.run(record.path, "switch", "-c", request.name, *switch_start)
        else:
            branch_start = [request.start_point] if request.start_point else []
            await gitops.run(record.path, "branch", "--end-of-options", request.name, *branch_start)
        await self.refresh(record)
        return Done(message=f"created {request.name}")
