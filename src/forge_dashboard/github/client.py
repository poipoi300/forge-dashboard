"""An async client for GitHub's API that spends as little of the rate limit as it can.

- GETs are conditional: the response's ETag is stored in the `github_cache`
  table and sent back as `If-None-Match`, and a 304 answer reuses the stored
  body. GitHub does not count 304 answers against the rate limit.
- Every response updates the remaining budget per rate-limit resource. Polling
  asks permission first: once the budget drops under the reserve, polls are
  refused (`RateLimitedError`) while the user's own requests (`essential`)
  keep working until the budget is truly gone.
- Writes and GraphQL queries are separate methods and never touch the cache.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from forge_dashboard.core.db import Database
from forge_dashboard.github.auth import GitHubAuth

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_S = 20.0
CORE = "core"
GRAPHQL = "graphql"

QueryParams = Mapping[str, str | int | bool]


class GitHubError(Exception):
    """A failed GitHub request. `status` is the HTTP status, or 0 when GitHub never answered."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class UnavailableError(GitHubError):
    """GitHub could not be reached."""


class UnauthorizedError(GitHubError):
    """GitHub rejected the credential, or the request needs one."""


class NotFoundError(GitHubError):
    """The resource does not exist, or is private and the credential cannot see it."""


class UnexpectedResponseError(GitHubError):
    """GitHub answered with something other than the documented shape."""


class RateLimitedError(GitHubError):
    """The rate limit is exhausted, or too low to spend on background polling."""

    def __init__(self, message: str, status: int = 0, reset_at: float | None = None) -> None:
        super().__init__(message, status)
        self.reset_at = reset_at


@dataclass(frozen=True)
class RateBudget:
    """One rate-limit resource as GitHub last reported it. `reset_at` is epoch seconds."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None


@dataclass(frozen=True)
class Page[ItemT]:
    """Items from consecutive pages; `more_available` when the last page we were willing to read was full."""

    items: list[ItemT]
    more_available: bool


class GitHubClient:
    """GitHub's REST and GraphQL API, with ETag caching and rate-limit accounting.

    Args:
        database: Holds the `github_cache` table.
        auth: The credential to send; assign `auth` again after re-resolving it.
        reserve_fraction: Share of a resource's limit kept back from background polling.
        on_budget_changed: Called after any response that reported the rate limit.
        base_url: The API root.
    """

    def __init__(
        self,
        database: Database,
        auth: GitHubAuth,
        *,
        reserve_fraction: float = 0.1,
        on_budget_changed: Callable[[], None] | None = None,
        base_url: str = API_URL,
    ) -> None:
        self.database = database
        self.auth = auth
        self.reserve_fraction = reserve_fraction
        self.on_budget_changed = on_budget_changed
        self.base_url = base_url
        self._budgets: dict[str, RateBudget] = {}
        self._http = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "forge-dashboard",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def budget(self) -> RateBudget:
        """The REST budget; unknown until the first response."""
        return self._budgets.get(CORE, RateBudget())

    # --- reads ---

    async def get_json(self, path: str, params: QueryParams | None = None, *, essential: bool = False) -> JsonValue:
        """GET `path` through the ETag cache.

        Args:
            path: API path, such as `/repos/o/r`.
            params: Query parameters; part of the cache key.
            essential: A request the user is waiting on: allowed to dip into the polling reserve.

        Raises:
            RateLimitedError: when the budget is spent (or, for polls, under the reserve).
            GitHubError: for any other failed or unexpected response.
        """
        self._guard_budget(CORE, essential)
        url = self._url(path, params)
        cached = await self.database.fetch_one("SELECT etag, body FROM github_cache WHERE url = ?", (url,))
        conditional = {"If-None-Match": cached["etag"]} if cached is not None and cached["etag"] else {}
        response = await self._send("GET", url, headers=conditional)
        if response.status_code == httpx.codes.NOT_MODIFIED:
            if cached is None:
                raise UnexpectedResponseError("GitHub answered 304 to a request that was not conditional", 304)
            body: JsonValue = json.loads(cached["body"])
            return body
        self._raise_for_status(response)
        payload = self._payload(response)
        etag = response.headers.get("ETag")
        if etag:
            await self.database.execute(
                "INSERT INTO github_cache (url, etag, status, body, fetched_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (url) DO UPDATE SET etag = excluded.etag, status = excluded.status, "
                "body = excluded.body, fetched_at = excluded.fetched_at",
                (url, etag, response.status_code, json.dumps(payload), time.time()),
            )
        return payload

    async def get_model[ModelT: BaseModel](
        self, path: str, model: type[ModelT], params: QueryParams | None = None, *, essential: bool = False
    ) -> ModelT:
        return validate_payload(path, model, await self.get_json(path, params, essential=essential))

    async def get_models[ModelT: BaseModel](
        self, path: str, model: type[ModelT], params: QueryParams | None = None, *, essential: bool = False
    ) -> list[ModelT]:
        """GET an endpoint that returns a JSON array of `model`."""
        return validate_list(path, model, await self.get_json(path, params, essential=essential))

    async def get_pages[ModelT: BaseModel](
        self,
        path: str,
        model: type[ModelT],
        params: QueryParams | None = None,
        *,
        page_size: int = 100,
        max_pages: int = 3,
        essential: bool = False,
    ) -> Page[ModelT]:
        """GET up to `max_pages` pages of an array endpoint.

        Pages are requested by number, not by following `Link` headers, because
        a cached 304 answer does not carry them.
        """
        items: list[ModelT] = []
        for number in range(1, max_pages + 1):
            batch = await self.get_models(
                path, model, {**(params or {}), "per_page": page_size, "page": number}, essential=essential
            )
            items.extend(batch)
            if len(batch) < page_size:
                return Page(items, more_available=False)
        return Page(items, more_available=True)

    # --- writes and GraphQL: never cached ---

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> JsonValue:
        return await self._write("POST", path, body)

    async def patch(self, path: str, body: Mapping[str, JsonValue]) -> JsonValue:
        return await self._write("PATCH", path, body)

    async def put(self, path: str, body: Mapping[str, JsonValue]) -> JsonValue:
        return await self._write("PUT", path, body)

    async def graphql[ModelT: BaseModel](
        self, query: str, variables: Mapping[str, JsonValue], model: type[ModelT], *, essential: bool = False
    ) -> ModelT:
        """Run a GraphQL query and validate its `data` as `model`.

        Raises:
            UnauthorizedError: when unauthenticated; GitHub's GraphQL API requires a token.
        """
        if not self.auth.authenticated:
            raise UnauthorizedError("GitHub's GraphQL API needs a token", 401)
        self._guard_budget(GRAPHQL, essential)
        response = await self._send("POST", self._url("/graphql", None), json={"query": query, "variables": variables})
        self._raise_for_status(response)
        document = self._payload(response)
        if not isinstance(document, dict):
            raise UnexpectedResponseError("GitHub's GraphQL API returned something other than an object")
        errors = document.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message", "GraphQL query failed"))
            if first.get("type") == "NOT_FOUND":
                raise NotFoundError(message, 404)
            raise GitHubError(message, response.status_code)
        return validate_payload("/graphql", model, document.get("data"))

    async def _write(self, method: str, path: str, body: Mapping[str, JsonValue]) -> JsonValue:
        self._guard_budget(CORE, essential=True)
        response = await self._send(method, self._url(path, None), json=body)
        self._raise_for_status(response)
        return self._payload(response)

    # --- cache upkeep ---

    async def prune_cache(self, max_age_s: float) -> None:
        """Forget cached responses older than `max_age_s`."""
        await self.database.execute("DELETE FROM github_cache WHERE fetched_at < ?", (time.time() - max_age_s,))

    # --- plumbing ---

    def _url(self, path: str, params: QueryParams | None) -> str:
        return str(httpx.URL(f"{self.base_url}{path}", params=sorted((params or {}).items())))

    async def _send(
        self, method: str, url: str, *, headers: Mapping[str, str] | None = None, json: object = None
    ) -> httpx.Response:
        try:
            response = await self._http.request(
                method, url, headers={**self.auth.headers(), **(headers or {})}, json=json
            )
        except httpx.TransportError as error:
            raise UnavailableError(f"could not reach GitHub ({type(error).__name__})") from error
        self._track_budget(response)
        return response

    def _track_budget(self, response: httpx.Response) -> None:
        headers = response.headers
        limit, remaining, reset = (headers.get(f"X-RateLimit-{name}") for name in ("Limit", "Remaining", "Reset"))
        if limit is None or remaining is None or reset is None:
            return
        resource = headers.get("X-RateLimit-Resource", CORE)
        self._budgets[resource] = RateBudget(limit=int(limit), remaining=int(remaining), reset_at=float(reset))
        if self.on_budget_changed is not None:
            self.on_budget_changed()

    def _guard_budget(self, resource: str, essential: bool) -> None:
        budget = self._budgets.get(resource, RateBudget())
        if budget.limit is None or budget.remaining is None or budget.reset_at is None:
            return
        if time.time() >= budget.reset_at:
            return  # the window rolled over; the next response reports the new numbers
        floor = 0 if essential else math.ceil(budget.limit * self.reserve_fraction)
        if budget.remaining > floor:
            return
        kind = "exhausted" if budget.remaining == 0 else f"under the {floor} request reserve for background polling"
        raise RateLimitedError(f"GitHub's {resource} rate limit is {kind}", reset_at=budget.reset_at)

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < httpx.codes.BAD_REQUEST:
            return
        message = _error_message(response)
        if status == httpx.codes.TOO_MANY_REQUESTS or (
            status == httpx.codes.FORBIDDEN and _is_rate_limit(response, message)
        ):
            raise RateLimitedError(message, status, reset_at=_reset_at(response))
        if status == httpx.codes.UNAUTHORIZED:
            raise UnauthorizedError(message, status)
        if status == httpx.codes.NOT_FOUND:
            raise NotFoundError(message, status)
        raise GitHubError(f"{message} (HTTP {status})", status)

    @staticmethod
    def _payload(response: httpx.Response) -> JsonValue:
        if not response.content:
            return None
        try:
            payload: JsonValue = response.json()
        except ValueError as error:
            raise UnexpectedResponseError("GitHub returned a body that is not JSON", response.status_code) from error
        return payload


def validate_payload[ModelT: BaseModel](path: str, model: type[ModelT], payload: object) -> ModelT:
    """`payload` as a `model`.

    Raises:
        UnexpectedResponseError: when GitHub's answer does not have the documented shape.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise UnexpectedResponseError(
            f"unexpected response from GitHub for {path}: {location}: {first['msg']}"
        ) from error


def validate_list[ModelT: BaseModel](path: str, model: type[ModelT], payload: object) -> list[ModelT]:
    """`payload` as a list of `model`; same failure as `validate_payload`."""
    if not isinstance(payload, list):
        raise UnexpectedResponseError(f"GitHub returned {type(payload).__name__}, not a list, for {path}")
    return [validate_payload(path, model, item) for item in payload]


def _error_message(response: httpx.Response) -> str:
    reason = response.reason_phrase or f"HTTP {response.status_code}"
    try:
        document: Any = response.json()
    except ValueError:
        return reason
    if not isinstance(document, dict):
        return reason
    message = str(document.get("message") or reason)
    details = [
        f"{error.get('field', error.get('resource', ''))}: {error.get('code', error.get('message', ''))}"
        for error in document.get("errors", [])
        if isinstance(error, dict)
    ]
    return f"{message} ({'; '.join(details)})" if details else message


def _is_rate_limit(response: httpx.Response, message: str) -> bool:
    return (
        response.headers.get("X-RateLimit-Remaining") == "0"
        or "Retry-After" in response.headers
        or "rate limit" in message.lower()
    )


def _reset_at(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        return time.time() + float(retry_after)
    reset = response.headers.get("X-RateLimit-Reset")
    return float(reset) if reset is not None else None
