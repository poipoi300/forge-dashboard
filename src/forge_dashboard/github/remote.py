"""Recognise a GitHub repository in a git remote URL."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from forge_dashboard.process import tree

# `ssh.github.com:443` is GitHub's documented SSH-over-HTTPS endpoint.
_HOSTS = frozenset({"github.com", "ssh.github.com"})
_URL_SCHEMES = frozenset({"https", "http", "ssh", "git"})
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>\S+)$")
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]*"
GITHUB_OWNER_PATTERN = rf"^{_OWNER}$"
GITHUB_SLUG_PATTERN = rf"^{_OWNER}/[A-Za-z0-9._-]+$"
_OWNER_AND_NAME = re.compile(GITHUB_SLUG_PATTERN)


def parse_github_slug(remote_url: str) -> str | None:
    """`owner/name` for a github.com remote URL, or None when the remote is not on GitHub.

    Understands `https://github.com/o/r(.git)`, `git@github.com:o/r.git`, and
    `ssh://git@github.com/o/r.git`, with or without credentials in the URL.
    The URL itself is never returned, so a token embedded in it cannot leak.
    """
    url = remote_url.strip()
    if "://" in url:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme not in _URL_SCHEMES:
            return None
        host, path = parts.hostname, parts.path
    else:
        scp_like = _SCP_LIKE.match(url)
        if scp_like is None:
            return None
        host, path = scp_like["host"], scp_like["path"]
    if host is None or host.lower() not in _HOSTS:
        return None
    slug = path.strip("/").removesuffix(".git")
    return slug if _OWNER_AND_NAME.fullmatch(slug) else None


async def origin_github_slug(checkout: Path) -> str | None:
    """Resolve the checkout's current GitHub origin without exposing its URL."""
    completed = await tree.run(
        ["git", "-C", str(checkout), "--git-dir=.git", "remote", "get-url", "origin"], checkout, timeout_s=10.0
    )
    return parse_github_slug(completed.stdout) if completed.ok else None
