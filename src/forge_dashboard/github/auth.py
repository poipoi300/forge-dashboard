"""GitHub authentication: where a token came from, never what it is.

`resolve_auth` walks the chain in order and stops at the first source that
yields a token, so there is no manual step whenever any source exists:

1. `GH_TOKEN` or `GITHUB_TOKEN` in the environment;
2. `gh auth token`, when the GitHub CLI is installed;
3. the git credential helper, asked non-interactively so no prompt can appear;
4. a token the user saved through the dashboard, kept in the OS keyring;
5. unauthenticated (public repositories only, at a far lower rate limit).

A `GitHubAuth` exposes its token only as request headers. It has no
attribute dictionary and a repr that names the source alone, so it cannot
leak through logging, serialisation, or a failed assertion.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import keyring
from keyring.errors import KeyringError

from forge_dashboard.process import tree

KEYRING_SERVICE = "forge-dashboard"
KEYRING_USERNAME = "github"
COMMAND_TIMEOUT_S = 10.0

_ENVIRONMENT_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")
_CREDENTIAL_REQUEST = b"protocol=https\nhost=github.com\n\n"
# GCM_INTERACTIVE and GCM_GUI_PROMPT keep Git Credential Manager from opening a window.
_CREDENTIAL_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GCM_GUI_PROMPT": "false"}

_log = logging.getLogger("forge_dashboard.github.auth")


class AuthSource(StrEnum):
    ENVIRONMENT = "environment"
    GH_CLI = "gh-cli"
    GIT_CREDENTIAL = "git-credential"
    KEYRING = "keyring"
    NONE = "none"


class GitHubAuth:
    """A resolved credential: its source, and the headers that carry the token."""

    __slots__ = ("_token", "source")

    def __init__(self, source: AuthSource, token: str | None) -> None:
        if (token is None) != (source is AuthSource.NONE):
            raise ValueError("a token comes with every source except 'none', and with no other")
        self.source = source
        self._token = token

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def headers(self) -> dict[str, str]:
        """The `Authorization` header, or nothing when unauthenticated."""
        return {"Authorization": f"Bearer {self._token}"} if self._token is not None else {}

    def __repr__(self) -> str:
        return f"GitHubAuth(source={self.source.value!r})"


class CommandRunner(Protocol):
    """The shape of `tree.run`, so tests can stand in for the real processes."""

    async def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        *,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
    ) -> tree.CompletedRun: ...


async def resolve_auth(environ: Mapping[str, str] | None = None, run: CommandRunner | None = None) -> GitHubAuth:
    """Find a GitHub token by walking the auth chain; unauthenticated when nothing yields one.

    Args:
        environ: The environment to read; defaults to the process environment.
        run: Runs `gh` and `git`; defaults to `tree.run`.
    """
    environment = os.environ if environ is None else environ
    runner = tree.run if run is None else run

    token = _token_from_environment(environment)
    if token is not None:
        return GitHubAuth(AuthSource.ENVIRONMENT, token)
    token = await _token_from_gh_cli(runner)
    if token is not None:
        return GitHubAuth(AuthSource.GH_CLI, token)
    token = await _token_from_git_credential(runner)
    if token is not None:
        return GitHubAuth(AuthSource.GIT_CREDENTIAL, token)
    token = await load_saved_token()
    if token is not None:
        return GitHubAuth(AuthSource.KEYRING, token)
    return GitHubAuth(AuthSource.NONE, None)


def _token_from_environment(environ: Mapping[str, str]) -> str | None:
    for name in _ENVIRONMENT_VARIABLES:
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


async def _stdout_of(
    runner: CommandRunner, argv: Sequence[str], env: Mapping[str, str] | None = None, stdin: bytes | None = None
) -> str | None:
    """Standard output of a command that succeeded, or None when it is missing, failed, or timed out."""
    try:
        completed = await runner(argv, Path.home(), env, timeout_s=COMMAND_TIMEOUT_S, stdin=stdin)
    except tree.ExecutableNotFoundError:
        return None
    return completed.stdout if completed.ok else None


async def _token_from_gh_cli(runner: CommandRunner) -> str | None:
    output = await _stdout_of(runner, ["gh", "auth", "token"])
    token = output.strip() if output is not None else ""
    return token or None


async def _token_from_git_credential(runner: CommandRunner) -> str | None:
    output = await _stdout_of(
        runner, ["git", "credential", "fill"], tree.child_env(_CREDENTIAL_ENV), _CREDENTIAL_REQUEST
    )
    if output is None:
        return None
    for line in output.splitlines():
        field, _, value = line.partition("=")
        if field == "password" and value:
            return value
    return None


# --- the token the user saved through the dashboard ---


async def load_saved_token() -> str | None:
    """The token saved in the OS keyring, or None when there is none or no keyring is usable."""
    try:
        return await asyncio.to_thread(keyring.get_password, KEYRING_SERVICE, KEYRING_USERNAME)
    except KeyringError as error:
        _log.warning("OS keyring unavailable (%s); skipping the saved-token source", type(error).__name__)
        return None


async def store_token(token: str) -> None:
    """Store `token` in the OS keyring, replacing any saved one.

    Raises:
        keyring.errors.KeyringError: when no keyring backend can store it.
    """
    await asyncio.to_thread(keyring.set_password, KEYRING_SERVICE, KEYRING_USERNAME, token)


async def forget_token() -> bool:
    """Remove the saved token; returns whether there was one.

    Raises:
        keyring.errors.KeyringError: when the keyring cannot be read or written.
    """
    if await asyncio.to_thread(keyring.get_password, KEYRING_SERVICE, KEYRING_USERNAME) is None:
        return False
    await asyncio.to_thread(keyring.delete_password, KEYRING_SERVICE, KEYRING_USERNAME)
    return True
