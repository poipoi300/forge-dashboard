"""Maintainer-owned automation settings, shared by checkouts of the same GitHub repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self, override

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from forge_dashboard.core.service import NoInput, Service
from forge_dashboard.core.ui import ui
from forge_dashboard.github.remote import GITHUB_OWNER_PATTERN, GITHUB_SLUG_PATTERN, origin_github_slug

if TYPE_CHECKING:
    from forge_dashboard.services.projects import ProjectRecord

AutomationAction = Annotated[
    Literal[
        "push",
        "open-pull-request",
        "merge-branch",
        "create-issue",
        "comment",
        "close-issue",
        "reopen-issue",
        "set-labels",
        "rerun-failed-jobs",
        "queue-issue",
    ],
    Field(
        json_schema_extra=ui(
            enum_labels={
                "push": "Push",
                "open-pull-request": "Open pull request",
                "merge-branch": "Merge branch",
                "create-issue": "Create issue",
                "comment": "Comment",
                "close-issue": "Close issue",
                "reopen-issue": "Reopen issue",
                "set-labels": "Set labels",
                "rerun-failed-jobs": "Re-run failed jobs",
                "queue-issue": "Queue issue",
            }
        )
    ),
]
ProviderRoute = Literal["claude", "codex", "grok"]
GitHubSlug = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=GITHUB_SLUG_PATTERN)]
GitHubLogin = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=GITHUB_OWNER_PATTERN)]


class RepoAutomationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: GitHubSlug = Field(
        title="GitHub repository",
        description="Repository from the origin remote, in owner/name form.",
        json_schema_extra=ui(placeholder="owner/name"),
    )
    owner: GitHubLogin = Field(
        title="Maintainer login", description="GitHub login of the maintainer for this repository."
    )
    enabled: bool = Field(False, strict=True, title="Enable automation")
    automatic_actions: tuple[AutomationAction, ...] = Field(
        (), description="Actions allowed automatically when automation is enabled. All other actions require approval."
    )
    provider_route: ProviderRoute = Field(
        title="Provider route",
        description="Provider account to use for this repository's automated model calls.",
        json_schema_extra=ui(enum_labels={"claude": "Claude", "codex": "Codex", "grok": "Grok"}),
    )

    def allows_automatic(self, action: AutomationAction) -> bool:
        """Whether the repo policy permits this action to proceed without approval."""
        return self.enabled and action in self.automatic_actions


class AutomationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repositories: tuple[RepoAutomationPolicy, ...] = Field((), json_schema_extra=ui(columns=["repo", "enabled"]))

    @model_validator(mode="after")
    def _unique_repositories(self) -> Self:
        if len({policy.repo for policy in self.repositories}) != len(self.repositories):
            raise ValueError("configure each GitHub repository only once")
        return self


class AutomationService(Service[AutomationConfig, NoInput]):
    id = "automation"
    title = "Automation"
    description = "Set the maintainer, action approvals, and provider route for each GitHub repository."
    Config = AutomationConfig
    State = NoInput

    @override
    async def update_config(self, config: AutomationConfig) -> AutomationConfig:
        """Activate policy only after the maintainer's settings were persisted."""
        await self._persist_config(config)
        previous = self.config
        self.config = config
        await self.on_config_changed(previous)
        return config

    def policy_for_repo(self, repo: str) -> RepoAutomationPolicy | None:
        """Look up the actual target repository. Unconfigured targets have no automatic permissions."""
        key = repo.lower()
        return next((policy for policy in self.config.repositories if policy.repo == key), None)

    async def policy_for_project(self, record: ProjectRecord) -> RepoAutomationPolicy | None:
        """Read the live origin so an edited remote cannot inherit another repository's policy."""
        if not record.is_git:
            return None
        repo = await origin_github_slug(record.path)
        return self.policy_for_repo(repo) if repo is not None else None
