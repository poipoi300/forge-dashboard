"""The GitHub client: ETag caching, rate-limit accounting and gating, error mapping, pagination, writes, GraphQL."""

import math
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import BaseModel

from forge_dashboard.core.db import Database
from forge_dashboard.github.auth import AuthSource, GitHubAuth
from forge_dashboard.github.client import (
    GitHubClient,
    GitHubError,
    NotFoundError,
    RateBudget,
    RateLimitedError,
    UnauthorizedError,
    UnavailableError,
    UnexpectedResponseError,
)
from forge_dashboard.github.fetch import fetch_pulls
from forge_dashboard.github.payloads import IssuePayload, RepoPayload

# Not a credential: a marker that must never show up in output.
SECRET = "fake-token-must-never-leak"
ANONYMOUS = GitHubAuth(AuthSource.NONE, None)
SIGNED_IN = GitHubAuth(AuthSource.ENVIRONMENT, SECRET)


class Widget(BaseModel):
    id: int
    name: str


def rate_headers(
    remaining: int, limit: int = 60, reset_in_s: float = 3600.0, resource: str | None = None
) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(int(time.time() + reset_in_s)),
    }
    if resource is not None:
        headers["X-RateLimit-Resource"] = resource
    return headers


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as router:
        yield router


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    opened = await Database.open(tmp_path / "cache.sqlite3")
    yield opened
    await opened.close()


@pytest.fixture
async def client(database: Database) -> AsyncIterator[GitHubClient]:
    created = GitHubClient(database, ANONYMOUS)
    yield created
    await created.aclose()


async def cached_urls(database: Database) -> list[str]:
    return [row["url"] for row in await database.fetch_all("SELECT url FROM github_cache ORDER BY url")]


# --- ETag cache ---


async def test_a_304_answer_reuses_the_cached_body(github: respx.MockRouter, client: GitHubClient) -> None:
    route = github.get("/widgets/1").mock(
        side_effect=[
            httpx.Response(200, json={"id": 1, "name": "first"}, headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
    )
    assert await client.get_json("/widgets/1") == {"id": 1, "name": "first"}
    assert await client.get_json("/widgets/1") == {"id": 1, "name": "first"}
    assert "if-none-match" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["if-none-match"] == '"v1"'


async def test_a_changed_etag_replaces_the_cached_body(github: respx.MockRouter, client: GitHubClient) -> None:
    route = github.get("/widgets/1").mock(
        side_effect=[
            httpx.Response(200, json={"id": 1, "name": "old"}, headers={"ETag": '"v1"'}),
            httpx.Response(200, json={"id": 1, "name": "new"}, headers={"ETag": '"v2"'}),
            httpx.Response(304),
        ]
    )
    await client.get_json("/widgets/1")
    assert await client.get_json("/widgets/1") == {"id": 1, "name": "new"}
    assert await client.get_json("/widgets/1") == {"id": 1, "name": "new"}
    assert route.calls[2].request.headers["if-none-match"] == '"v2"'


async def test_responses_without_an_etag_are_not_cached(
    github: respx.MockRouter, client: GitHubClient, database: Database
) -> None:
    github.get("/widgets/1").respond(200, json={"id": 1, "name": "x"})
    await client.get_json("/widgets/1")
    assert await cached_urls(database) == []


async def test_query_parameters_are_part_of_the_cache_key_in_a_stable_order(
    github: respx.MockRouter, client: GitHubClient, database: Database
) -> None:
    github.get("/widgets").respond(200, json=[], headers={"ETag": '"e"'})
    await client.get_json("/widgets", {"state": "open", "page": 1})
    await client.get_json("/widgets", {"page": 1, "state": "open"})
    await client.get_json("/widgets", {"state": "closed", "page": 1})
    assert await cached_urls(database) == [
        "https://api.github.com/widgets?page=1&state=closed",
        "https://api.github.com/widgets?page=1&state=open",
    ]


async def test_a_304_answer_to_an_unconditional_request_is_a_protocol_violation(
    github: respx.MockRouter, client: GitHubClient
) -> None:
    github.get("/widgets/1").respond(304)
    with pytest.raises(UnexpectedResponseError, match="not conditional"):
        await client.get_json("/widgets/1")


async def test_old_cache_rows_are_pruned(database: Database, client: GitHubClient) -> None:
    now = time.time()
    await database.execute(
        "INSERT INTO github_cache (url, etag, status, body, fetched_at) VALUES ('old', 'e', 200, '1', ?), "
        "('fresh', 'e', 200, '1', ?)",
        (now - 100, now),
    )
    await client.prune_cache(max_age_s=50)
    assert await cached_urls(database) == ["fresh"]


# --- rate limit accounting and the polling gate ---


async def test_every_response_updates_the_budget_and_notifies(github: respx.MockRouter, database: Database) -> None:
    changes: list[int | None] = []
    watched = GitHubClient(database, ANONYMOUS, on_budget_changed=lambda: changes.append(watched.budget.remaining))
    github.get("/widgets/1").respond(200, json={}, headers=rate_headers(remaining=57, reset_in_s=100))
    assert watched.budget == RateBudget()
    await watched.get_json("/widgets/1")
    assert (watched.budget.limit, watched.budget.remaining) == (60, 57)
    assert watched.budget.reset_at == pytest.approx(time.time() + 100, abs=5)
    assert changes == [57]
    await watched.aclose()


async def test_responses_without_rate_limit_headers_leave_the_budget_unknown(
    github: respx.MockRouter, client: GitHubClient
) -> None:
    github.get("/widgets/1").respond(200, json={})
    await client.get_json("/widgets/1")
    assert client.budget.remaining is None


async def test_low_budget_refuses_polls_without_sending_a_request(
    github: respx.MockRouter, client: GitHubClient
) -> None:
    route = github.get("/widgets/1").respond(200, json={}, headers=rate_headers(remaining=6, limit=60))
    await client.get_json("/widgets/1")
    with pytest.raises(RateLimitedError, match="reserve") as refused:
        await client.get_json("/widgets/1")
    assert route.call_count == 1
    assert refused.value.reset_at == client.budget.reset_at


async def test_a_users_own_request_may_spend_the_reserve(github: respx.MockRouter, client: GitHubClient) -> None:
    route = github.get("/widgets/1").respond(200, json={}, headers=rate_headers(remaining=3, limit=60))
    await client.get_json("/widgets/1")
    await client.get_json("/widgets/1", essential=True)
    assert route.call_count == 2


async def test_an_exhausted_budget_refuses_even_essential_requests(
    github: respx.MockRouter, client: GitHubClient
) -> None:
    route = github.get("/widgets/1").respond(200, json={}, headers=rate_headers(remaining=0))
    await client.get_json("/widgets/1")
    with pytest.raises(RateLimitedError, match="exhausted"):
        await client.get_json("/widgets/1", essential=True)
    with pytest.raises(RateLimitedError):
        await client.post("/widgets", {})
    assert route.call_count == 1


async def test_the_reserve_follows_the_configured_fraction(github: respx.MockRouter, client: GitHubClient) -> None:
    client.reserve_fraction = 0.5
    route = github.get("/widgets/1").respond(200, json={}, headers=rate_headers(remaining=31, limit=60))
    await client.get_json("/widgets/1")
    await client.get_json("/widgets/1")
    assert math.ceil(60 * client.reserve_fraction) == 30
    client.reserve_fraction = 0.6
    with pytest.raises(RateLimitedError):
        await client.get_json("/widgets/1")
    assert route.call_count == 2


async def test_polling_resumes_once_the_window_has_reset(github: respx.MockRouter, client: GitHubClient) -> None:
    route = github.get("/widgets/1").respond(200, json={}, headers=rate_headers(remaining=0, reset_in_s=-1))
    await client.get_json("/widgets/1")
    await client.get_json("/widgets/1")
    assert route.call_count == 2


async def test_graphql_has_its_own_budget(github: respx.MockRouter, database: Database) -> None:
    signed_in = GitHubClient(database, SIGNED_IN)
    github.post("/graphql").respond(
        200, json={"data": {"id": 1, "name": "x"}}, headers=rate_headers(remaining=4990, limit=5000, resource="graphql")
    )
    github.get("/widgets/1").respond(200, json={}, headers=rate_headers(remaining=50, resource="core"))
    await signed_in.graphql("{ x }", {}, Widget)
    await signed_in.get_json("/widgets/1")
    assert (signed_in.budget.limit, signed_in.budget.remaining) == (60, 50)
    await signed_in.aclose()


# --- errors ---


async def test_rate_limit_errors_carry_the_reset_time(github: respx.MockRouter, client: GitHubClient) -> None:
    headers = rate_headers(remaining=0, reset_in_s=500)
    github.get("/a").respond(403, json={"message": "API rate limit exceeded"}, headers=headers)
    with pytest.raises(RateLimitedError) as exhausted:
        await client.get_json("/a", essential=True)
    assert exhausted.value.reset_at == float(headers["X-RateLimit-Reset"])


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (403, {"Retry-After": "30"}, "secondary"),
        (429, {}, "slow down"),
        (403, {}, "You have exceeded a secondary rate limit"),
    ],
    ids=["retry-after", "429", "message-only"],
)
async def test_other_rate_limit_signals_are_recognised(
    github: respx.MockRouter, database: Database, status: int, headers: dict[str, str], message: str
) -> None:
    fresh = GitHubClient(database, ANONYMOUS)
    github.get("/a").respond(status, json={"message": message}, headers=headers)
    with pytest.raises(RateLimitedError) as limited:
        await fresh.get_json("/a")
    if "Retry-After" in headers:
        assert limited.value.reset_at == pytest.approx(time.time() + 30, abs=5)
    await fresh.aclose()


async def test_a_forbidden_response_that_is_not_a_rate_limit_stays_a_plain_error(
    github: respx.MockRouter, client: GitHubClient
) -> None:
    github.get("/a").respond(403, json={"message": "Resource not accessible"}, headers=rate_headers(remaining=40))
    with pytest.raises(GitHubError, match=r"Resource not accessible \(HTTP 403\)") as forbidden:
        await client.get_json("/a")
    assert not isinstance(forbidden.value, RateLimitedError) and forbidden.value.status == 403


async def test_status_codes_map_to_error_types(github: respx.MockRouter, client: GitHubClient) -> None:
    github.get("/missing").respond(404, json={"message": "Not Found"})
    github.get("/denied").respond(401, json={"message": "Bad credentials"})
    github.get("/broken").respond(500, text="<html>oops</html>")
    with pytest.raises(NotFoundError):
        await client.get_json("/missing")
    with pytest.raises(UnauthorizedError, match="Bad credentials"):
        await client.get_json("/denied")
    with pytest.raises(GitHubError, match=r"\(HTTP 500\)") as broken:
        await client.get_json("/broken")
    assert broken.value.status == 500


async def test_validation_failures_list_the_offending_fields(github: respx.MockRouter, client: GitHubClient) -> None:
    github.post("/widgets").respond(
        422,
        json={
            "message": "Validation Failed",
            "errors": [{"resource": "Issue", "field": "title", "code": "missing_field"}, "stray"],
        },
    )
    with pytest.raises(GitHubError, match=r"Validation Failed \(title: missing_field\)") as failed:
        await client.post("/widgets", {})
    assert failed.value.status == 422


async def test_an_unreachable_github_is_reported_not_raised_raw(github: respx.MockRouter, client: GitHubClient) -> None:
    github.get("/a").mock(side_effect=httpx.ConnectError("dns failure with details"))
    with pytest.raises(UnavailableError, match=r"could not reach GitHub \(ConnectError\)"):
        await client.get_json("/a")


async def test_unexpected_shapes_fail_loudly_with_the_path_and_field(
    github: respx.MockRouter, client: GitHubClient
) -> None:
    github.get("/widgets/1").respond(200, json={"id": "not-a-number", "name": "x"})
    github.get("/widgets").respond(200, json={"id": 1})
    github.get("/html").respond(200, text="<html></html>")
    with pytest.raises(UnexpectedResponseError, match=r"/widgets/1: id: "):
        await client.get_model("/widgets/1", Widget)
    with pytest.raises(UnexpectedResponseError, match="not a list"):
        await client.get_models("/widgets", Widget)
    with pytest.raises(UnexpectedResponseError, match="not JSON"):
        await client.get_json("/html")


# --- pagination ---


async def test_pages_are_read_by_number_until_a_short_page(github: respx.MockRouter, client: GitHubClient) -> None:
    pages = {"1": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}], "2": [{"id": 3, "name": "c"}]}
    route = github.get("/widgets").mock(
        side_effect=lambda request: httpx.Response(200, json=pages[request.url.params["page"]])
    )
    page = await client.get_pages("/widgets", Widget, {"state": "open"}, page_size=2)
    assert [item.id for item in page.items] == [1, 2, 3] and not page.more_available
    assert [call.request.url.params["per_page"] for call in route.calls] == ["2", "2"]
    assert all(call.request.url.params["state"] == "open" for call in route.calls)


async def test_a_full_last_page_within_the_page_limit_reports_more_available(
    github: respx.MockRouter, client: GitHubClient
) -> None:
    route = github.get("/widgets").respond(200, json=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
    page = await client.get_pages("/widgets", Widget, page_size=2, max_pages=2)
    assert len(page.items) == 4 and page.more_available and route.call_count == 2


# --- writes ---


async def test_writes_send_json_with_the_token_and_never_touch_the_cache(
    github: respx.MockRouter, database: Database
) -> None:
    signed_in = GitHubClient(database, SIGNED_IN)
    created = github.post("/widgets").respond(201, json={"id": 1, "name": "made"}, headers={"ETag": '"e"'})
    patched = github.patch("/widgets/1").respond(200, json={"id": 1, "name": "patched"})
    replaced = github.put("/widgets/1/tags").respond(200, json=[{"name": "x"}])
    assert await signed_in.post("/widgets", {"name": "made", "tags": ["a"]}) == {"id": 1, "name": "made"}
    assert await signed_in.patch("/widgets/1", {"state": "closed"}) == {"id": 1, "name": "patched"}
    assert await signed_in.put("/widgets/1/tags", {"labels": ["x"]}) == [{"name": "x"}]
    assert created.calls[0].request.content == b'{"name":"made","tags":["a"]}'
    assert patched.calls[0].request.content == b'{"state":"closed"}'
    assert replaced.calls[0].request.content == b'{"labels":["x"]}'
    for route in (created, patched, replaced):
        request = route.calls[0].request
        assert request.headers["authorization"] == f"Bearer {SECRET}" and "if-none-match" not in request.headers
    assert await cached_urls(database) == []
    await signed_in.aclose()


async def test_an_empty_write_response_is_none(github: respx.MockRouter, client: GitHubClient) -> None:
    github.post("/widgets/1/rerun").respond(204)
    assert await client.post("/widgets/1/rerun", {}) is None


async def test_requests_carry_a_token_only_when_there_is_one(
    github: respx.MockRouter, client: GitHubClient, database: Database
) -> None:
    route = github.get("/widgets/1").respond(200, json={})
    await client.get_json("/widgets/1")
    assert "authorization" not in route.calls[0].request.headers
    client.auth = SIGNED_IN
    await client.get_json("/widgets/1")
    assert route.calls[1].request.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in repr(client.auth) and SECRET not in repr(client.budget)
    assert route.calls[1].request.headers["x-github-api-version"] == "2022-11-28"
    assert route.calls[1].request.headers["accept"] == "application/vnd.github+json"


# --- GraphQL ---


async def test_graphql_needs_a_token_and_sends_nothing_without_one(
    github: respx.MockRouter, client: GitHubClient
) -> None:
    route = github.post("/graphql").respond(200, json={"data": {}})
    with pytest.raises(UnauthorizedError, match="needs a token"):
        await client.graphql("{ x }", {}, Widget)
    assert route.call_count == 0


async def test_graphql_returns_validated_data(github: respx.MockRouter, database: Database) -> None:
    signed_in = GitHubClient(database, SIGNED_IN)
    route = github.post("/graphql").respond(200, json={"data": {"id": 2, "name": "gql"}})
    result = await signed_in.graphql("query($n: Int) { x }", {"n": 1}, Widget)
    assert result == Widget(id=2, name="gql")
    assert route.calls[0].request.content == b'{"query":"query($n: Int) { x }","variables":{"n":1}}'
    await signed_in.aclose()


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}, NotFoundError),
        ({"errors": [{"message": "Field 'x' doesn't exist"}]}, GitHubError),
        ({"errors": ["odd"]}, GitHubError),
    ],
    ids=["not-found", "query-error", "malformed-error"],
)
async def test_graphql_errors_arrive_in_a_200_and_are_raised(
    github: respx.MockRouter, database: Database, document: dict[str, object], expected: type[GitHubError]
) -> None:
    signed_in = GitHubClient(database, SIGNED_IN)
    github.post("/graphql").respond(200, json=document)
    with pytest.raises(expected):
        await signed_in.graphql("{ x }", {}, Widget)
    await signed_in.aclose()


async def test_graphql_answers_that_are_not_objects_are_rejected(github: respx.MockRouter, database: Database) -> None:
    signed_in = GitHubClient(database, SIGNED_IN)
    github.post("/graphql").respond(200, json=[1])
    with pytest.raises(UnexpectedResponseError, match="other than an object"):
        await signed_in.graphql("{ x }", {}, Widget)
    await signed_in.aclose()


# --- live ---


@pytest.fixture
async def anonymous_live_client(database: Database, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[GitHubClient]:
    """An unauthenticated client on the real network; the test is skipped when GitHub is unreachable or spent."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    live = GitHubClient(database, GitHubAuth(AuthSource.NONE, None))
    try:
        await live.get_json("/rate_limit", essential=True)  # free: does not count against the limit
    except UnavailableError:
        await live.aclose()
        pytest.skip("GitHub is not reachable")
    if live.budget.remaining is not None and live.budget.remaining < 5:
        await live.aclose()
        pytest.skip("the unauthenticated rate limit is spent")
    yield live
    await live.aclose()


@pytest.mark.live
async def test_public_repository_metadata_issues_and_pulls_parse_from_real_payloads(
    anonymous_live_client: GitHubClient,
) -> None:
    repo = await anonymous_live_client.get_model("/repos/octocat/Hello-World", RepoPayload)
    assert (repo.full_name, repo.private, repo.fork) == ("octocat/Hello-World", False, False)
    assert repo.html_url == "https://github.com/octocat/Hello-World" and repo.default_branch
    issues = await anonymous_live_client.get_pages(
        "/repos/octocat/Hello-World/issues", IssuePayload, {"state": "open"}, max_pages=1
    )
    assert all(issue.number > 0 and issue.title for issue in issues.items)
    pulls = await fetch_pulls(anonymous_live_client, "octocat/Hello-World", "open", limit=5, check_limit=0)
    assert all(pull.checks is None for pull in pulls.pulls) and pulls.total >= len(pulls.pulls)
    assert not anonymous_live_client.auth.authenticated and anonymous_live_client.budget.limit == 60
