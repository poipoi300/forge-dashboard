"""Compliance with forge's standards, bring-up to standards, and adopting unmanifested repos.

The forge repo is the standards source and is read live on every evaluation.
Bring-up runs on a `forge-standards/<stamp>` worktree branch: deterministic
fixes first (CLAUDE.md, .gitignore, links to forge's working-rule sections,
the template revision; hooks are installed in the main repo), then the
harness with a generated brief for what only judgment can fix, then
compliance and the project's checks again, and a diff for one-click merge.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, override

import tomlkit
from pydantic import BaseModel, Field

from forge_dashboard import gitops, worktrees
from forge_dashboard.branchwork import BranchRunState, CheckOutcomeBrief, Score
from forge_dashboard.core.events import Event, Severity
from forge_dashboard.core.service import ActionError, JobRef, ProjectRef, Service, action
from forge_dashboard.core.tasks import BackgroundTasks
from forge_dashboard.core.ui import ui
from forge_dashboard.domain import standards as rules
from forge_dashboard.domain.standards import ComplianceReport, ForgeSource
from forge_dashboard.domain.summary import StandardsFacts
from forge_dashboard.manifest import load_manifest
from forge_dashboard.manifest.models import MANIFEST_FILENAME
from forge_dashboard.manifest.schema import manifest_json_schema_text
from forge_dashboard.services.agents import AgentsService, base_branch_of
from forge_dashboard.services.checks import ChecksService
from forge_dashboard.services.discovery import ChangeKind
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService, JobStatus
from forge_dashboard.services.projects import ProjectListener, ProjectRecord, ProjectsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime

CONTRACT_DOC = Path(__file__).resolve().parents[3] / "docs" / "project-manifest.md"
ADOPT_ATTEMPTS = 3
EVALUATE_DEBOUNCE_S = 1.0
# The working-rule sections of forge's AGENTS.md that every project links at its pinned commit.
TRAVELLING_SECTIONS = ["A note from the maintainer", "Verifying", "Commits", "Taste"]


class ForgeCommit(BaseModel):
    short_sha: str = Field(json_schema_extra=ui(format="code"))
    subject: str
    committed_at: float = Field(json_schema_extra=ui(format="relative-time"))


class ProjectStandards(BaseModel):
    project_id: str
    report: ComplianceReport | None = None
    forge_commits_since: list[ForgeCommit] = Field(
        default_factory=list, json_schema_extra=ui(columns=["short_sha", "subject", "committed_at"])
    )
    behind: int | None = Field(None, json_schema_extra=ui(help="forge commits since the template revision."))
    evaluated_at: float | None = Field(None, json_schema_extra=ui(format="relative-time"))
    error: str | None = None


class StandardsConfig(BaseModel):
    travelling_sections: list[str] = Field(
        default_factory=lambda: list(TRAVELLING_SECTIONS),
        json_schema_extra=ui(widget="tags", help="forge's AGENTS.md sections every project must link."),
    )


class StandardsState(BaseModel):
    forge_repo: str | None = Field(None, json_schema_extra=ui(format="path"))
    forge_head: str | None = Field(None, json_schema_extra=ui(format="code"))
    forge_head_subject: str | None = None
    projects: list[ProjectStandards] = Field(default_factory=list)


class StandardsAligned(Event):
    type: ClassVar[str] = "standards.evaluated"
    persist: ClassVar[bool] = True
    passing: int
    total: int
    previous_passing: int | None

    def title(self) -> str:
        return f"Standards: {self.passing}/{self.total} checks passing"

    def severity(self) -> Severity:
        return "success" if self.passing == self.total else "warning"

    def dedupe_key(self) -> str:
        return f"standards:{self.project_id}:{self.passing}/{self.total}:{int(time.time() // 3600)}"


def _hooks_python(repo: Path) -> list[str]:
    """pre-commit from the project's existing env, else the dashboard's (installing writes only .git/hooks)."""
    if (repo / ".venv").is_dir() and (repo / "uv.lock").is_file():
        return ["uv", "run", "--no-sync", "pre-commit"]
    if shutil.which("pre-commit") is not None:
        return ["pre-commit"]
    return [sys.executable, "-m", "pre_commit"]


def set_template_revision(manifest_text: str, revision: str, aligned_at: datetime) -> str:
    """Write `[forge]` with the revision and time, preserving every other byte of the manifest."""
    document = tomlkit.parse(manifest_text)
    table = document.get("forge")
    if table is None:
        table = tomlkit.table()
        document["forge"] = table
    table["template_revision"] = revision
    table["aligned_at"] = aligned_at.replace(microsecond=0)
    return tomlkit.dumps(document)


def standards_brief(
    *,
    project_name: str,
    languages: list[str],
    failing: list[rules.ComplianceCheck],
    forge: ForgeSource,
    travelling: list[str],
    slug: str,
    revision: str | None,
) -> str:
    """The prompt for the judgment part of a bring-up."""
    pin = revision or "<forge's HEAD commit>"
    hooks = "\n".join(f"- {language}: {', '.join(ids)}" for language, ids in rules.LANGUAGE_HOOKS.items())
    failing_text = "\n".join(
        f"- {check.id}: {check.title}. {check.detail}" + (f"\n```diff\n{check.diff}\n```" if check.diff else "")
        for check in failing
    )
    rules_text = "\n".join(
        [
            (
                "- Rewrite AGENTS.md's project-specific sections for this project (the intro, what makes it special, "
                "the glossary, the ways to hurt yourself, commands, and where code lives)."
            ),
            (
                f"- Never copy forge's files or sections into the project. AGENTS.md links forge's working-rule "
                f"sections ({', '.join(travelling)}); the dashboard already wrote those links."
            ),
            (
                f"- Reference forge's published hooks (`.pre-commit-hooks.yaml` below) from .pre-commit-config.yaml "
                f"as `repo: https://github.com/{slug}` with `rev: {pin}`. Keep project-specific gates as local hooks."
            ),
            (
                f"- CI is a workflow whose job is `uses: {slug}/{rules.FORGE_WORKFLOW}@{pin}`, passing only the "
                "project's inputs (OS matrix, system packages, toolchains). Both pins name the same commit."
            ),
            (
                "- Adapt the gates to the project's languages using the conventional hook ids, and keep the push stage "
                f"as the single hook `{rules.PUSH_HOOK}`."
            ),
            "- Never weaken an existing gate: no lowered thresholds, added excludes, or blanket ignores.",
            "- When you change .pre-commit-config.yaml or CI, update the gate registry and its mirror tests too.",
            f'- [forge].template_revision = "{revision}" is already set in forge.project.toml; keep it.'
            if revision
            else "- Set [forge].template_revision in forge.project.toml to forge's HEAD.",
            "- Edit files only. Do not run git; the dashboard commits and verifies.",
        ]
    )
    forge_parts = [
        ("FORGE_AGENTS_MD", "AGENTS.md"),
        ("FORGE_PRECOMMIT", ".pre-commit-config.yaml"),
        ("FORGE_HOOKS", ".pre-commit-hooks.yaml"),
        ("FORGE_GATES_WORKFLOW", rules.FORGE_WORKFLOW),
        ("FORGE_PYPROJECT", "pyproject.toml"),
        ("FORGE_GITIGNORE", ".gitignore"),
    ]
    forge_text = "\n\n".join(
        f"## forge's {name}\n<<<{marker}\n{(forge.text(name) or '').rstrip()}\n{marker}>>>"
        for marker, name in forge_parts
    )
    return (
        "\n\n".join(
            [
                "# Bring up to standards",
                (
                    f"You are bringing {project_name} up to forge's engineering standards. The current directory is an "
                    "isolated git worktree of the project."
                ),
                f"Project languages: {', '.join(languages) or 'not declared'}.",
                f"## Failing checks\n{failing_text or '- none'}",
                f"## Rules\n{rules_text}",
                f"## Conventional hook ids per language\n{hooks}",
                forge_text,
            ]
        )
        + "\n"
    )


def adopt_brief(*, listing: str, readme: str, contract: str, schema: str, problems: str | None) -> str:
    """The prompt for writing a first manifest from what the repository contains."""
    parts = [
        "# Adopt",
        (
            "Write forge.project.toml at the root of this repository, describing it accurately from its files: "
            "identity, languages, V1 milestones that reflect what exists and what is missing, the checks its gates run "
            "(argv arrays, JUnit where the test runner supports it), and services it can launch."
        ),
        "Edit only forge.project.toml. Do not run git or any command.",
        f"## Files in the repository\n{listing}",
        f"## README\n{readme or '(none)'}",
        f"## The manifest contract\n{contract}",
        f"## JSON Schema\n```json\n{schema}\n```",
    ]
    if problems:
        parts.append(f"## Previous attempt failed validation\nFix every error:\n{problems}")
    return "\n\n".join(parts) + "\n"


class StandardsService(Service[StandardsConfig, StandardsState], ProjectListener):
    id = "standards"
    title = "Standards"
    description = "Compliance against forge, read live; bring-up to standards and adopt, on worktree branches."
    Config = StandardsConfig
    State = StandardsState
    events = (StandardsAligned,)

    def __init__(
        self,
        runtime: Runtime,
        projects: ProjectsService,
        jobs: JobsService,
        checks: ChecksService,
        agents: AgentsService,
    ) -> None:
        super().__init__(runtime)
        self._projects = projects
        self._jobs = jobs
        self._checks = checks
        self._agents = agents
        self._reports: dict[str, ProjectStandards] = {}
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._background = BackgroundTasks()
        projects.listen(self)

    @override
    async def stop(self) -> None:
        for handle in self._pending.values():
            handle.cancel()
        await self._background.cancel_all()

    # --- forge ---

    def _forge(self) -> ForgeSource | None:
        root = self._projects.forge_repo
        return ForgeSource(root) if root is not None and (root / "AGENTS.md").is_file() else None

    def _require_forge(self) -> ForgeSource:
        forge = self._forge()
        if forge is None:
            raise ActionError("the forge repo was not found; set its path in Discovery settings", status=409)
        return forge

    async def _forge_head(self, forge: ForgeSource) -> tuple[str | None, str | None]:
        commits = await gitops.log(forge.root, limit=1, stats=False)
        return (commits[0].sha, commits[0].subject) if commits else (None, None)

    # --- evaluation ---

    @override
    async def project_upserted(self, record: ProjectRecord) -> None:
        self._background.spawn(self.evaluate_project(record), name=f"standards:{record.id}")

    @override
    async def project_removed(self, project_id: str) -> None:
        self._reports.pop(project_id, None)
        self._publish_state()

    @override
    async def files_changed(self, record: ProjectRecord, kinds: frozenset[ChangeKind]) -> None:
        targets = self._projects.records() if record.role == "standards-source" else [record]
        for target in targets:
            self._schedule_evaluation(target)

    def _schedule_evaluation(self, record: ProjectRecord) -> None:
        """Debounce evaluations: a burst of edits evaluates once."""
        pending = self._pending.pop(record.id, None)
        if pending is not None:
            pending.cancel()

        def evaluate() -> None:
            self._background.spawn(self.evaluate_project(record), name=f"standards:{record.id}")

        self._pending[record.id] = asyncio.get_running_loop().call_later(EVALUATE_DEBOUNCE_S, evaluate)

    async def evaluate_project(self, record: ProjectRecord) -> ProjectStandards:
        """Evaluate one project (the forge repo itself is the source, not evaluated)."""
        forge = self._forge()
        if record.role == "standards-source" or forge is None:
            standing = ProjectStandards(
                project_id=record.id,
                error=None if record.role == "standards-source" else "the forge repo was not found",
                evaluated_at=time.time(),
            )
            self._reports[record.id] = standing
            self._publish_state(forge)
            return standing
        head, _subject = await self._forge_head(forge)
        report = await asyncio.to_thread(
            rules.evaluate, record.path, forge, travelling_sections=self.config.travelling_sections, forge_head=head
        )
        commits, behind = await self._commits_since(forge, report.template_revision)
        previous = self._reports.get(record.id)
        standing = ProjectStandards(
            project_id=record.id, report=report, forge_commits_since=commits, behind=behind, evaluated_at=time.time()
        )
        self._reports[record.id] = standing
        if previous is None or previous.report is None or previous.report.passing != report.passing:
            self.publish(
                StandardsAligned(
                    project_id=record.id,
                    passing=report.passing,
                    total=report.total,
                    previous_passing=previous.report.passing if previous and previous.report else None,
                )
            )
        self._projects.put(
            record.id,
            StandardsFacts(
                passing=report.passing,
                total=report.total,
                failing=[check.id for check in report.failing],
                drift=bool(report.failing) or behind is None or behind > 0,
                forge_commits_behind=behind,
            ),
        )
        await self._publish_state_async(forge)
        return standing

    async def _commits_since(self, forge: ForgeSource, revision: str | None) -> tuple[list[ForgeCommit], int | None]:
        if revision is None or not await gitops.ref_exists(forge.root, f"{revision}^{{commit}}"):
            return [], None
        found = await gitops.log(forge.root, f"{revision}..HEAD", limit=100, stats=False)
        return [ForgeCommit(short_sha=c.short_sha, subject=c.subject, committed_at=c.committed_at) for c in found], len(
            found
        )

    async def _publish_state_async(self, forge: ForgeSource | None) -> None:
        head, subject = await self._forge_head(forge) if forge is not None else (None, None)
        self.set_state(
            StandardsState(
                forge_repo=str(forge.root) if forge is not None else None,
                forge_head=head,
                forge_head_subject=subject,
                projects=sorted(self._reports.values(), key=lambda standing: standing.project_id),
            )
        )

    def _publish_state(self, forge: ForgeSource | None = None) -> None:
        self._background.spawn(self._publish_state_async(forge or self._forge()), name="standards-state")

    # --- deterministic fixes ---

    async def apply_deterministic(self, worktree: Path, forge: ForgeSource, revision: str | None) -> list[str]:
        """Fix what needs no judgment; returns what changed."""
        changed: list[str] = []
        claude = worktree / "CLAUDE.md"
        if not claude.is_file():
            claude.write_text(forge.text("CLAUDE.md") or "@AGENTS.md\n", encoding="utf-8", newline="\n")
            changed.append("CLAUDE.md")
        elif not any(line.strip() == "@AGENTS.md" for line in claude.read_text(encoding="utf-8").splitlines()):
            claude.write_text("@AGENTS.md\n\n" + claude.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            changed.append("CLAUDE.md")
        ignore = worktree / ".gitignore"
        current = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
        missing = rules.missing_gitignore_lines(current, forge.text(".gitignore") or "")
        if missing:
            prefix = current if not current or current.endswith("\n") else current + "\n"
            ignore.write_text(prefix + "".join(f"{line}\n" for line in missing), encoding="utf-8", newline="\n")
            changed.append(".gitignore")
        agents = worktree / "AGENTS.md"
        slug = forge.slug()
        if agents.is_file() and slug is not None and revision is not None:
            text = agents.read_text(encoding="utf-8")
            linked = rules.apply_rule_links(text, slug, revision, self.config.travelling_sections)
            if linked != text:
                agents.write_text(linked, encoding="utf-8", newline="\n")
                changed.append("AGENTS.md (links to forge's working rules)")
        manifest = worktree / MANIFEST_FILENAME
        if revision is not None and manifest.is_file() and load_manifest(worktree).valid:
            updated = set_template_revision(manifest.read_text(encoding="utf-8"), revision, datetime.now(tz=UTC))
            if updated != manifest.read_text(encoding="utf-8"):
                manifest.write_text(updated, encoding="utf-8", newline="\n")
                changed.append(f"{MANIFEST_FILENAME} [forge]")
        return changed

    async def _install_hooks(self, context: JobContext, record: ProjectRecord) -> bool:
        if not record.is_git:
            return False
        argv = [*_hooks_python(record.path), "install", "--allow-missing-config", "-t", "pre-commit", "-t", "pre-push"]
        result = await context.run(argv, record.path, timeout_s=300)
        return result.ok

    # --- actions ---

    @action(title="Evaluate standards")
    async def evaluate(self, request: ProjectRef) -> ProjectStandards:
        """Re-run every compliance check against forge's current text."""
        return await self.evaluate_project(self._projects.record(request.project_id))

    @action(title="Standards report", query=True)
    async def report(self, request: ProjectRef) -> ProjectStandards:
        """The latest compliance report with per-check detail and diffs."""
        standing = self._reports.get(request.project_id)
        if standing is None:
            return await self.evaluate_project(self._projects.record(request.project_id))
        return standing

    @action(title="Bring up to standards", long_running=True, confirm=True)
    async def bring_up(self, request: ProjectRef) -> JobRef:
        """Deterministic fixes, then the agent, on a forge-standards branch; review the diff, then merge."""
        record = self._projects.record(request.project_id)
        if not record.is_git:
            raise ActionError(f"{record.path} is not a git repository", status=409)
        forge = self._require_forge()
        slug = forge.slug()
        if slug is None:
            raise ActionError("forge's checkout has no GitHub origin remote for projects to reference", status=409)
        if forge.published_hooks("HEAD") is None:
            raise ActionError("forge publishes no .pre-commit-hooks.yaml at its HEAD", status=409)

        async def work(context: JobContext) -> JobOutcome:
            branch_work = self._agents.branch_work
            run, worktree = await branch_work.start(
                repo=record.path,
                project_id=record.id,
                kind="standards",
                title=f"Bring {record.id} up to standards",
                branch_prefix="forge-standards",
                base_branch=await base_branch_of(record),
                job_id=context.job_id,
            )
            async with self._agents.guard_branch_run(run, record.path):
                head, _subject = await self._forge_head(forge)
                travelling = self.config.travelling_sections
                before = await asyncio.to_thread(
                    rules.evaluate, worktree.path, forge, travelling_sections=travelling, forge_head=head
                )
                context.log(f"before: {before.passing}/{before.total} checks passing")
                changed = await self.apply_deterministic(worktree.path, forge, head)
                hooks = await self._install_hooks(context, record)
                context.log(f"deterministic fixes: {', '.join(changed) or 'none'}; hooks installed: {hooks}")
                await worktrees.commit_all(worktree.path, "Apply forge standards: deterministic fixes")
                middle = await asyncio.to_thread(
                    rules.evaluate, worktree.path, forge, travelling_sections=travelling, forge_head=head
                )
                agent_needed = [check for check in middle.failing if check.fix == "agent"]
                agent_ids: list[str] = []
                if agent_needed:
                    manifest = load_manifest(worktree.path).manifest
                    prompt = standards_brief(
                        project_name=manifest.project.name if manifest else record.path.name,
                        languages=list(manifest.project.languages) if manifest else record.languages,
                        failing=agent_needed,
                        forge=forge,
                        travelling=travelling,
                        slug=slug,
                        revision=head,
                    )
                    agent = await self._agents.run_agent(
                        context, kind="standards", project_id=record.id, prompt=prompt, cwd=worktree.path
                    )
                    agent_ids.append(agent.run_id)
                    if agent.outcome != "succeeded":
                        run = await branch_work.finish(
                            run.model_copy(update={"agent_run_ids": agent_ids}),
                            record.path,
                            summary="agent run failed",
                            error=agent.outcome,
                        )
                        return JobOutcome(status=JobStatus.FAILED, summary=f"agent {agent.outcome}")
                    await self.apply_deterministic(worktree.path, forge, head)
                    await worktrees.commit_all(worktree.path, "Bring the repo up to forge's standards")
                after = await asyncio.to_thread(
                    rules.evaluate, worktree.path, forge, travelling_sections=travelling, forge_head=head
                )
                manifest_after = load_manifest(worktree.path).manifest
                checks = list(manifest_after.checks) if manifest_after is not None else []
                results = await self._checks.run_checks_in(
                    context, record, checks, worktree.path, record_results=False, on_head=True, sha=None, dirty=False
                )
                summary = f"standards {before.passing}/{before.total} -> {after.passing}/{after.total}"
                run = run.model_copy(
                    update={
                        "agent_run_ids": agent_ids,
                        "checks": [CheckOutcomeBrief(check_id=c.id, label=c.label, outcome=o) for c, o in results],
                        "agent_attempts": len(agent_ids),
                        "compliance_before": Score(passing=before.passing, total=before.total),
                        "compliance_after": Score(passing=after.passing, total=after.total),
                        "still_failing": [check.id for check in after.failing],
                        "hooks_installed": hooks,
                        "deterministic_fixes": changed,
                    }
                )
                run = await branch_work.finish(run, record.path, summary=summary)
                await self.evaluate_project(record)
                if run.state is not BranchRunState.READY:
                    return JobOutcome(status=JobStatus.FAILED, summary=run.error or summary)
                return JobOutcome(status=JobStatus.ATTENTION, summary=f"{summary}; review and merge")

        return await self._jobs.submit(
            kind="standards.bring-up",
            title=f"Bring {record.id} up to standards",
            work=work,
            project_id=record.id,
            resources=[f"repo:{record.id}"],
        )

    @action(title="Adopt", long_running=True, confirm=True)
    async def adopt(self, request: ProjectRef) -> JobRef:
        """The agent writes forge.project.toml from the repo, validated with a repair loop, on a branch."""
        record = self._projects.record(request.project_id)
        if not record.is_git:
            raise ActionError(f"{record.path} is not a git repository", status=409)
        if record.has_manifest_file:
            raise ActionError("this project already has a forge.project.toml", status=409)

        async def work(context: JobContext) -> JobOutcome:
            branch_work = self._agents.branch_work
            run, worktree = await branch_work.start(
                repo=record.path,
                project_id=record.id,
                kind="adopt",
                title=f"Adopt {record.id}",
                branch_prefix="forge-adopt",
                base_branch=await base_branch_of(record),
                job_id=context.job_id,
            )
            async with self._agents.guard_branch_run(run, record.path):
                listing = await gitops.output(worktree.path, "ls-files")
                readme_path = worktree.path / "README.md"
                readme = (
                    readme_path.read_text(encoding="utf-8", errors="replace")[:6000] if readme_path.is_file() else ""
                )
                contract = CONTRACT_DOC.read_text(encoding="utf-8") if CONTRACT_DOC.is_file() else ""
                problems: str | None = None
                agent_ids: list[str] = []
                for attempt in range(1, ADOPT_ATTEMPTS + 1):
                    prompt = adopt_brief(
                        listing="\n".join(listing.splitlines()[:400]),
                        readme=readme,
                        contract=contract,
                        schema=manifest_json_schema_text(),
                        problems=problems,
                    )
                    agent = await self._agents.run_agent(
                        context, kind="adopt", project_id=record.id, prompt=prompt, cwd=worktree.path
                    )
                    agent_ids.append(agent.run_id)
                    if agent.outcome != "succeeded":
                        # The repair loop is for a manifest the agent got wrong, not for a run that failed outright.
                        run = await branch_work.finish(
                            run.model_copy(update={"agent_run_ids": agent_ids, "agent_attempts": len(agent_ids)}),
                            record.path,
                            summary="agent run failed",
                            error=agent.outcome,
                        )
                        return JobOutcome(status=JobStatus.FAILED, summary=f"agent {agent.outcome}")
                    result = load_manifest(worktree.path)
                    if result.valid:
                        context.log(f"manifest valid after {attempt} attempt(s)")
                        problems = None
                        break
                    problems = "\n".join(issue.render(MANIFEST_FILENAME) for issue in result.issues)
                    context.log(f"attempt {attempt}: manifest invalid:\n{problems}")
                await worktrees.commit_all(worktree.path, "Add forge.project.toml")
                run = run.model_copy(update={"agent_run_ids": agent_ids, "agent_attempts": len(agent_ids)})
                run = await branch_work.finish(
                    run,
                    record.path,
                    summary="manifest valid" if problems is None else "invalid",
                    error=None if problems is None else problems,
                )
                if problems is not None:
                    return JobOutcome(
                        status=JobStatus.FAILED, summary=f"no valid manifest after {ADOPT_ATTEMPTS} attempts"
                    )
                return JobOutcome(
                    status=JobStatus.ATTENTION, summary="manifest written; review, merge, then bring up to standards"
                )

        return await self._jobs.submit(
            kind="standards.adopt",
            title=f"Adopt {record.id}",
            work=work,
            project_id=record.id,
            resources=[f"repo:{record.id}"],
        )
