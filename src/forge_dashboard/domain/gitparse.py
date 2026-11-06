"""Parsers for git's machine-readable output formats.

Every command the dashboard runs asks git for a stable format (porcelain v2,
NUL-separated fields, `--format` with explicit separators), so parsing is
exact rather than scraped from human output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# Separators as they appear in output. Format strings spell them with git's
# escapes (`%x00`, `%00`) because a NUL cannot appear in a Windows command line.
FIELD = "\x00"
RECORD = "\x1e"

ChangeKind = Literal["modified", "added", "deleted", "renamed", "copied", "untracked", "conflicted", "type-changed"]

_STATUS_LETTER: dict[str, ChangeKind] = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type-changed",
}
_SHORTSTAT = re.compile(
    r"(?P<files>\d+) files? changed"
    r"(?:, (?P<insertions>\d+) insertions?\(\+\))?"
    r"(?:, (?P<deletions>\d+) deletions?\(-\))?"
)
_TRACK = re.compile(r"(?:ahead (?P<ahead>\d+))?(?:, )?(?:behind (?P<behind>\d+))?")


@dataclass(frozen=True)
class StatusEntry:
    path: str
    kind: ChangeKind
    index: str
    worktree: str
    original_path: str | None = None


@dataclass(frozen=True)
class BranchStatus:
    head: str | None
    oid: str | None
    upstream: str | None
    ahead: int | None
    behind: int | None


@dataclass(frozen=True)
class Status:
    branch: BranchStatus
    entries: tuple[StatusEntry, ...]

    @property
    def dirty(self) -> int:
        return len(self.entries)

    @property
    def conflicted(self) -> tuple[StatusEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == "conflicted")


def _kind(xy: str) -> ChangeKind:
    for letter in (xy[0], xy[1]):
        if letter in _STATUS_LETTER:
            return _STATUS_LETTER[letter]
    return "modified"


def parse_status_v2(output: str) -> Status:
    """Parse `git status --porcelain=v2 --branch -z`."""
    head: str | None = None
    oid: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    entries: list[StatusEntry] = []
    tokens = output.split(FIELD)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if token.startswith("# "):
            key, _, value = token[2:].partition(" ")
            if key == "branch.oid":
                oid = None if value == "(initial)" else value
            elif key == "branch.head":
                head = None if value == "(detached)" else value
            elif key == "branch.upstream":
                upstream = value
            elif key == "branch.ab":
                plus, minus = value.split(" ")
                ahead, behind = int(plus.lstrip("+")), int(minus.lstrip("-"))
            continue
        kind_marker = token[0]
        if kind_marker == "1":
            parts = token.split(" ", 8)
            entries.append(StatusEntry(path=parts[8], kind=_kind(parts[1]), index=parts[1][0], worktree=parts[1][1]))
        elif kind_marker == "2":
            parts = token.split(" ", 9)
            original = tokens[index]
            index += 1
            entries.append(
                StatusEntry(
                    path=parts[9],
                    kind=_kind(parts[1]),
                    index=parts[1][0],
                    worktree=parts[1][1],
                    original_path=original,
                )
            )
        elif kind_marker == "u":
            parts = token.split(" ", 10)
            entries.append(StatusEntry(path=parts[10], kind="conflicted", index=parts[1][0], worktree=parts[1][1]))
        elif kind_marker == "?":
            entries.append(StatusEntry(path=token[2:], kind="untracked", index="?", worktree="?"))
    return Status(
        branch=BranchStatus(head=head, oid=oid, upstream=upstream, ahead=ahead, behind=behind), entries=tuple(entries)
    )


@dataclass(frozen=True)
class Commit:
    sha: str
    short_sha: str
    author: str
    email: str
    committed_at: float
    subject: str
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


# The record separator leads each commit, so `--shortstat` lines land after their own header.
LOG_FORMAT = "%x1e%H%x00%h%x00%an%x00%ae%x00%ct%x00%s"


def parse_log(output: str) -> list[Commit]:
    """Parse `git log --format=LOG_FORMAT [--shortstat]` output."""
    commits: list[Commit] = []
    for chunk in output.split(RECORD)[1:]:
        header, _, stats = chunk.partition("\n")
        sha, short_sha, author, email, committed_at, subject = header.split(FIELD)
        match = _SHORTSTAT.search(stats)
        commits.append(
            Commit(
                sha=sha,
                short_sha=short_sha,
                author=author,
                email=email,
                committed_at=float(committed_at),
                subject=subject,
                files_changed=int(match["files"]) if match else 0,
                insertions=int(match["insertions"] or 0) if match else 0,
                deletions=int(match["deletions"] or 0) if match else 0,
            )
        )
    return commits


@dataclass(frozen=True)
class Branch:
    name: str
    is_head: bool
    upstream: str | None
    ahead: int | None
    behind: int | None
    gone: bool
    committed_at: float
    short_sha: str
    subject: str


BRANCH_FORMAT = (
    "%(HEAD)%00%(refname:short)%00%(upstream:short)%00%(upstream:track,nobracket)"
    "%00%(committerdate:unix)%00%(objectname:short)%00%(contents:subject)"
)


def parse_branches(output: str) -> list[Branch]:
    """Parse `git for-each-ref refs/heads --format=BRANCH_FORMAT`."""
    branches: list[Branch] = []
    for line in output.splitlines():
        if not line:
            continue
        head, name, upstream, track, committed_at, short_sha, subject = line.split(FIELD)
        gone = track == "gone"
        match = _TRACK.fullmatch(track) if track and not gone else None
        ahead = int(match["ahead"]) if match and match["ahead"] else (0 if upstream and not gone else None)
        behind = int(match["behind"]) if match and match["behind"] else (0 if upstream and not gone else None)
        branches.append(
            Branch(
                name=name,
                is_head=head == "*",
                upstream=upstream or None,
                ahead=ahead,
                behind=behind,
                gone=gone,
                committed_at=float(committed_at or 0),
                short_sha=short_sha,
                subject=subject,
            )
        )
    return branches


@dataclass(frozen=True)
class Worktree:
    path: str
    head: str | None
    branch: str | None
    bare: bool = False
    detached: bool = False
    locked: bool = False
    prunable: bool = False


def parse_worktrees(output: str) -> list[Worktree]:
    """Parse `git worktree list --porcelain`."""
    worktrees: list[Worktree] = []
    for block in output.strip().split("\n\n"):
        if not block.strip():
            continue
        values: dict[str, str] = {}
        flags: set[str] = set()
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if value:
                values[key] = value
            else:
                flags.add(key)
        branch = values.get("branch")
        worktrees.append(
            Worktree(
                path=values["worktree"],
                head=values.get("HEAD"),
                branch=branch.removeprefix("refs/heads/") if branch else None,
                bare="bare" in flags,
                detached="detached" in flags,
                locked="locked" in flags or "locked" in values,
                prunable="prunable" in flags or "prunable" in values,
            )
        )
    return worktrees


@dataclass(frozen=True)
class MergeTreeResult:
    """`git merge-tree --write-tree --name-only` outcome: clean, or the conflicted files."""

    clean: bool
    tree: str | None
    conflicted_files: tuple[str, ...] = field(default_factory=tuple)
    messages: tuple[str, ...] = field(default_factory=tuple)


def parse_merge_tree(output: str, exit_code: int) -> MergeTreeResult:
    """Exit 0 is clean; exit 1 lists conflicted paths after the tree id, then informational messages."""
    lines = output.splitlines()
    tree = lines[0] if lines else None
    if exit_code == 0:
        return MergeTreeResult(clean=True, tree=tree)
    files: list[str] = []
    rest = lines[1:]
    split = rest.index("") if "" in rest else len(rest)
    files = [line for line in rest[:split] if line]
    messages = tuple(line for line in rest[split + 1 :] if line)
    return MergeTreeResult(clean=False, tree=tree, conflicted_files=tuple(dict.fromkeys(files)), messages=messages)


_URL_USERINFO = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/?#]*)@")
_HTTP_SCHEMES = frozenset({"http", "https"})


def redact_url(url: str) -> str:
    """`url` without the `user:token@` part of its authority; scp-like and path remotes carry no userinfo."""
    return _URL_USERINFO.sub(r"\g<scheme>", url)


def has_embedded_credentials(url: str) -> bool:
    """Whether the URL carries a secret: a password anywhere, or any userinfo on http(s), where it is a token.

    A login like the `git@` of `ssh://git@host/...` is not a secret.
    """
    match = _URL_USERINFO.match(url)
    if match is None:
        return False
    scheme = match["scheme"].removesuffix("://").lower()
    return ":" in match["userinfo"] or scheme in _HTTP_SCHEMES


def parse_remotes(output: str) -> dict[str, str]:
    """Parse `git remote -v` into remote name -> fetch URL with any userinfo removed.

    Nothing downstream (state, API, reports, logs, commit messages) may see a
    token embedded in a remote; git commands that need the real URL address the remote by name.
    """
    remotes: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            remotes[parts[0]] = redact_url(parts[1])
    return remotes
