"""Who may talk to the API: the host names it is reached by, and the origins browsers may call it from.

The API runs code on this machine (launcher commands, merges, pushes, artifact deletion) and streams every state,
log, and transcript, so a web page the maintainer happens to visit must never be able to drive it. Two attacks
reach a loopback-only server through the browser:

- DNS rebinding re-resolves the attacker's own host name to 127.0.0.1, which makes the API same-origin for the page.
  The request still carries the attacker's name in `Host`, so only the names the dashboard is reached by are served.
- A cross-site page fetches or opens a WebSocket to the loopback address. Browsers always send `Origin` on those,
  so an `Origin` that is not one of ours is refused. A request without `Origin` (curl, the CLI, the runner's
  readiness probe) is not browser-borne and passes.

An origin is accepted for the same names as a host, with any port, so the Bun dev server on another loopback port
keeps working.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

_DNS_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")


def _ip_literal(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


def is_loopback_host(host: str) -> bool:
    """Whether binding `host` keeps the server reachable from this computer only."""
    literal = _ip_literal(host)
    return host == "localhost" or (literal is not None and literal.is_loopback)


def parse_host_name(text: str) -> str:
    """A bare host name or IP literal in the form `Host` and `Origin` carry it: lower-case, no brackets.

    Raises:
        ValueError: for a wildcard, a port, a scheme, a path, or anything else that is not exactly one host.
    """
    name = text.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    literal = _ip_literal(name)
    if literal is not None:
        return literal.compressed
    if name and all(_DNS_LABEL.fullmatch(label) for label in name.split(".")):
        return name
    raise ValueError(f"{text!r} is not a plain host name or IP address (no wildcard, port, scheme, or path)")


def origin_host(origin: str) -> str | None:
    """The host of an `Origin` header value; None for `null` and for anything that is not an http(s) origin."""
    try:
        parts = urlsplit(origin)
        host = parts.hostname
    except ValueError:
        return None
    return host if parts.scheme in ("http", "https") and host else None


class OriginGuard:
    """Refuse HTTP requests and WebSocket handshakes whose `Origin` is present and not one of `allowed_hosts`."""

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            origin = Headers(scope=scope).get("origin")
            if origin is not None and origin_host(origin) not in self.allowed_hosts:
                await PlainTextResponse("Forbidden origin", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def restrict_access(app: FastAPI, allowed_hosts: Iterable[str]) -> None:
    """Serve `app` only under `allowed_hosts` and only to origins on them.

    Raises:
        ValueError: when a name is not a plain host name or IP address (a wildcard would disable the check).
    """
    hosts = frozenset(parse_host_name(name) for name in allowed_hosts)
    app.add_middleware(OriginGuard, allowed_hosts=hosts)
    # Starlette keeps IPv6 literals in brackets when it parses `Host`; its `www.` redirect would answer a name we
    # never offered.
    patterns = [f"[{host}]" if ":" in host else host for host in sorted(hosts)]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=patterns, www_redirect=False)
