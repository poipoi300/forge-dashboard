"""The GitHub auth chain: order of sources, non-interactive credential helper, keyring, and token secrecy."""

import copy
import logging
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError
from pydantic import BaseModel

from forge_dashboard.github import auth
from forge_dashboard.github.auth import AuthSource, GitHubAuth, resolve_auth
from forge_dashboard.process import tree

# Not a credential: a marker that must never show up in output.
SECRET = "fake-token-must-never-leak"


class MemoryKeyring(KeyringBackend):
    """A keyring that lives in a dict, so no test reaches the operating system's store."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]  # keyring ships this initializer untyped
        self.secrets: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.secrets.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.secrets[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.secrets:
            raise PasswordDeleteError("no such password")
        del self.secrets[(service, username)]


def completed(stdout: str = "", exit_code: int | None = 0, *, timed_out: bool = False) -> tree.CompletedRun:
    return tree.CompletedRun(
        argv=(), exit_code=exit_code, stdout=stdout, stderr="", timed_out=timed_out, duration_s=0.0
    )


class FakeRunner:
    """Stands in for `tree.run`: answers `gh` and `git` from a table and records every call."""

    def __init__(self, answers: Mapping[str, tree.CompletedRun | Exception] | None = None) -> None:
        self.answers = dict(answers or {})
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None, bytes | None, float | None]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        *,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
    ) -> tree.CompletedRun:
        self.calls.append((tuple(argv), env, stdin, timeout_s))
        answer = self.answers.get(argv[0], tree.ExecutableNotFoundError(argv[0]))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def commands(self) -> list[str]:
        return [call[0][0] for call in self.calls]


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def _never_run_the_real_credential_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets to inject a runner fails instead of asking the machine's real helper for a token."""
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


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


# --- the chain, in order ---


async def test_gh_token_beats_github_token_and_nothing_else_is_asked() -> None:
    runner = FakeRunner()
    resolved = await resolve_auth({"GH_TOKEN": SECRET, "GITHUB_TOKEN": "other"}, runner)
    assert resolved.source is AuthSource.ENVIRONMENT and resolved.headers() == {"Authorization": f"Bearer {SECRET}"}
    assert runner.calls == []


@pytest.mark.parametrize("environment", [{"GITHUB_TOKEN": SECRET}, {"GH_TOKEN": "  ", "GITHUB_TOKEN": f" {SECRET}\n"}])
async def test_github_token_is_used_when_gh_token_is_missing_or_blank(environment: dict[str, str]) -> None:
    resolved = await resolve_auth(environment, FakeRunner())
    assert resolved.source is AuthSource.ENVIRONMENT and resolved.headers() == {"Authorization": f"Bearer {SECRET}"}


async def test_process_environment_is_read_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", SECRET)
    resolved = await resolve_auth()
    assert resolved.source is AuthSource.ENVIRONMENT


async def test_gh_cli_comes_before_the_git_credential_helper() -> None:
    runner = FakeRunner({"gh": completed(f"{SECRET}\n"), "git": completed("password=other\n")})
    resolved = await resolve_auth({}, runner)
    assert resolved.source is AuthSource.GH_CLI and resolved.headers() == {"Authorization": f"Bearer {SECRET}"}
    assert runner.calls[0][0] == ("gh", "auth", "token")
    assert runner.commands() == ["gh"]


@pytest.mark.parametrize(
    "gh_answer",
    [tree.ExecutableNotFoundError("gh"), completed("", exit_code=1), completed("  \n"), completed("x", timed_out=True)],
    ids=["not-installed", "not-logged-in", "empty-output", "timed-out"],
)
async def test_an_unusable_gh_cli_is_skipped_cleanly(gh_answer: tree.CompletedRun | Exception) -> None:
    runner = FakeRunner({"gh": gh_answer, "git": completed(f"username=x\npassword={SECRET}\n")})
    resolved = await resolve_auth({}, runner)
    assert resolved.source is AuthSource.GIT_CREDENTIAL
    assert runner.commands() == ["gh", "git"]


async def test_git_credential_helper_is_asked_without_any_prompt() -> None:
    runner = FakeRunner(
        {"git": completed(f"protocol=https\r\nhost=github.com\r\nusername=me\r\npassword={SECRET}\r\n")}
    )
    resolved = await resolve_auth({}, runner)
    assert resolved.source is AuthSource.GIT_CREDENTIAL and resolved.headers() == {"Authorization": f"Bearer {SECRET}"}
    argv, env, stdin, timeout_s = runner.calls[-1]
    assert argv == ("git", "credential", "fill")
    assert stdin == b"protocol=https\nhost=github.com\n\n"
    assert env is not None
    assert (env["GIT_TERMINAL_PROMPT"], env["GCM_INTERACTIVE"], env["GCM_GUI_PROMPT"]) == ("0", "never", "false")
    assert timeout_s == auth.COMMAND_TIMEOUT_S


@pytest.mark.parametrize(
    "git_answer",
    [
        completed("username=me\n"),
        completed("password=\n"),
        completed(f"password={SECRET}\n", exit_code=128),
        completed("", exit_code=None, timed_out=True),
        tree.ExecutableNotFoundError("git"),
    ],
    ids=["no-password-line", "empty-password", "helper-failed", "timed-out", "git-missing"],
)
async def test_a_credential_helper_without_a_token_falls_through_to_the_keyring(
    git_answer: tree.CompletedRun | Exception, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.secrets[(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME)] = SECRET
    resolved = await resolve_auth({}, FakeRunner({"git": git_answer}))
    assert resolved.source is AuthSource.KEYRING and resolved.authenticated


async def test_git_credential_helper_comes_before_the_saved_token(memory_keyring: MemoryKeyring) -> None:
    memory_keyring.secrets[(auth.KEYRING_SERVICE, auth.KEYRING_USERNAME)] = "saved"
    resolved = await resolve_auth({}, FakeRunner({"git": completed(f"password={SECRET}\n")}))
    assert resolved.source is AuthSource.GIT_CREDENTIAL


async def test_no_source_means_unauthenticated() -> None:
    resolved = await resolve_auth({}, FakeRunner())
    assert resolved.source is AuthSource.NONE and not resolved.authenticated and resolved.headers() == {}


async def test_an_unusable_keyring_is_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    keyring.set_keyring(fail.Keyring())  # type: ignore[no-untyped-call]  # untyped in keyring
    with caplog.at_level(logging.WARNING, logger="forge_dashboard.github.auth"):
        resolved = await resolve_auth({}, FakeRunner())
    assert resolved.source is AuthSource.NONE
    assert "OS keyring unavailable" in caplog.text


# --- the saved token ---


async def test_a_stored_token_is_found_and_can_be_forgotten() -> None:
    assert await auth.load_saved_token() is None
    await auth.store_token(SECRET)
    assert await auth.load_saved_token() == SECRET
    assert await auth.forget_token() is True
    assert await auth.load_saved_token() is None


async def test_forgetting_when_nothing_is_saved_reports_it() -> None:
    assert await auth.forget_token() is False


# --- secrecy ---


class Holder(BaseModel):
    source: AuthSource
    authenticated: bool


async def test_the_token_never_appears_in_text_form_or_serialisation(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        resolved = await resolve_auth({"GH_TOKEN": SECRET}, FakeRunner())
    holder = Holder(source=resolved.source, authenticated=resolved.authenticated)
    rendered = [
        repr(resolved),
        str(resolved),
        f"{resolved}",
        f"{resolved!r}",
        holder.model_dump_json(),
        repr(holder),
        caplog.text,
    ]
    assert all(SECRET not in text for text in rendered)
    assert repr(resolved) == "GitHubAuth(source='environment')"
    assert not hasattr(resolved, "__dict__")
    assert SECRET not in repr(copy.copy(resolved))


def test_a_token_goes_with_every_source_but_none() -> None:
    with pytest.raises(ValueError, match="comes with every source"):
        GitHubAuth(AuthSource.NONE, SECRET)
    with pytest.raises(ValueError, match="comes with every source"):
        GitHubAuth(AuthSource.KEYRING, None)
