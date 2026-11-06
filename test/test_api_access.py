"""The API answers only to loopback hosts and origins: DNS rebinding and cross-site pages cannot drive it."""

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from forge_dashboard.api.access import LOOPBACK_HOSTS, is_loopback_host, origin_host, parse_host_name
from forge_dashboard.api.app import create_app
from forge_dashboard.core.runtime import Runtime

BASE_URL = "http://127.0.0.1:8760"
STREAM_URL = "ws://127.0.0.1:8760/api/events"
PING = "/api/services/gallery/actions/ping"
GALLERY_STATE = "/api/services/gallery/state"
GALLERY_CONFIG = "/api/services/gallery/config"
FOREIGN_ORIGINS = [
    "https://evil.example",
    "http://evil.example:8760",
    "null",
    "http://localhost.evil.example",
    "http://127.0.0.1.evil.example:8760",
    "http://localhost@evil.example",
    "file://",
    "chrome-extension://abcdefgh",
]
DEV_SERVER_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://127.0.0.1:5173",
    "http://[::1]:5173",
    "http://LOCALHOST:5173",
]
REBINDING_HOSTS = ["evil.example", "evil.example:8760", "localhost.evil.example", "127.0.0.2:8760", "0.0.0.0:8760"]


@pytest.fixture
def client(make_runtime: Callable[[], Runtime]) -> Iterator[TestClient]:
    with TestClient(create_app(make_runtime()), base_url=BASE_URL) as test_client:
        yield test_client


def pings(client: TestClient) -> int:
    count: int = client.get(GALLERY_STATE).json()["state"]["pings"]
    return count


# --- names ---


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("localhost", "localhost"),
        (" Dashboard.LAN ", "dashboard.lan"),
        ("127.0.0.1", "127.0.0.1"),
        ("[::1]", "::1"),
        ("0:0:0:0:0:0:0:1", "::1"),
    ],
)
def test_host_names_are_normalised_the_way_hosts_and_origins_carry_them(text: str, name: str) -> None:
    assert parse_host_name(text) == name


@pytest.mark.parametrize(
    "text", ["*", "*.example.com", "dashboard.lan:8760", "http://dashboard.lan", "a/b", "", "-a", "a..b"]
)
def test_anything_but_one_plain_host_name_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="plain host name"):
        parse_host_name(text)


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("127.0.0.2", True),
        ("0.0.0.0", False),
        ("::", False),
        ("", False),
        ("192.168.1.5", False),
        ("dashboard.lan", False),
    ],
)
def test_only_loopback_addresses_keep_a_bound_server_on_this_computer(host: str, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://localhost:5173", "localhost"),
        ("https://[::1]:5173", "::1"),
        ("http://127.0.0.1", "127.0.0.1"),
        ("http://localhost@evil.example", "evil.example"),
        ("null", None),
        ("file://", None),
        ("chrome-extension://abcdefgh", None),
        ("http://[::1", None),
    ],
)
def test_an_origin_names_the_host_the_page_was_served_from(origin: str, host: str | None) -> None:
    assert origin_host(origin) == host


# --- Host ---


@pytest.mark.parametrize("host", REBINDING_HOSTS)
def test_a_rebinding_host_is_refused_for_reads_and_actions_alike(client: TestClient, host: str) -> None:
    assert client.get(GALLERY_STATE, headers={"Host": host}).status_code == 400
    assert client.get("/api/services", headers={"Host": host}).status_code == 400
    assert client.post(PING, json={}, headers={"Host": host}).status_code == 400
    assert client.put(GALLERY_CONFIG, json={}, headers={"Host": host}).status_code == 400
    assert pings(client) == 0


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:1", "localhost", "LOCALHOST:8760", "[::1]", "[::1]:8760"])
def test_every_spelling_of_a_loopback_host_with_any_port_is_served(client: TestClient, host: str) -> None:
    assert client.post(PING, json={}, headers={"Host": host}).status_code == 200


def test_health_answers_the_runners_loopback_readiness_probe(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Host": "127.0.0.1:49152"})

    assert response.status_code == 200 and response.json()["status"] == "ok"


def test_the_event_stream_refuses_a_rebinding_host(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(STREAM_URL, headers={"Host": "evil.example:8760"}),
    ):
        pytest.fail("the stream must not open for a foreign host")

    assert denied.value.status_code == 400


# --- Origin ---


@pytest.mark.parametrize("origin", FOREIGN_ORIGINS)
def test_a_foreign_origin_is_refused_and_nothing_runs(client: TestClient, origin: str) -> None:
    headers = {"Origin": origin}

    assert client.post(PING, json={}, headers=headers).status_code == 403
    assert client.put(GALLERY_CONFIG, json={}, headers=headers).status_code == 403
    assert client.get(GALLERY_STATE, headers=headers).status_code == 403
    assert client.options(PING, headers={**headers, "Access-Control-Request-Method": "POST"}).status_code == 403
    assert pings(client) == 0


@pytest.mark.parametrize("origin", FOREIGN_ORIGINS)
def test_the_event_stream_refuses_a_foreign_origin(client: TestClient, origin: str) -> None:
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(STREAM_URL, headers={"Origin": origin}),
    ):
        pytest.fail("the stream must not open for a foreign origin")

    assert denied.value.status_code == 403


@pytest.mark.parametrize("origin", DEV_SERVER_ORIGINS)
def test_a_loopback_dev_server_on_another_port_is_served(client: TestClient, origin: str) -> None:
    headers = {"Origin": origin}

    assert client.post(PING, json={}, headers=headers).status_code == 200
    assert pings(client) == 1
    with client.websocket_connect(STREAM_URL, headers=headers) as stream:
        assert stream.receive_json()["kind"] == "hello"


def test_requests_without_an_origin_are_served_as_the_cli_and_curl_send_them(client: TestClient) -> None:
    assert "origin" not in client.headers
    assert client.post(PING, json={}).status_code == 200
    with client.websocket_connect(STREAM_URL) as stream:
        assert stream.receive_json()["kind"] == "hello"


# --- opting in ---


@pytest.fixture
def exposed_client(make_runtime: Callable[[], Runtime]) -> Iterator[TestClient]:
    app = create_app(make_runtime(), allowed_hosts=[*LOOPBACK_HOSTS, "Dashboard.LAN"])
    with TestClient(app, base_url="http://dashboard.lan:8760") as test_client:
        yield test_client


def test_a_host_the_server_was_told_about_is_served_as_host_and_as_origin(exposed_client: TestClient) -> None:
    assert exposed_client.get("/api/health").status_code == 200
    assert exposed_client.post(PING, json={}, headers={"Origin": "http://dashboard.lan:8760"}).status_code == 200
    assert exposed_client.post(PING, json={}, headers={"Host": "127.0.0.1:8760"}).status_code == 200


def test_other_hosts_and_origins_stay_refused_when_one_more_is_allowed(exposed_client: TestClient) -> None:
    assert exposed_client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    assert exposed_client.post(PING, json={}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_a_wildcard_cannot_be_allowed(make_runtime: Callable[[], Runtime]) -> None:
    with pytest.raises(ValueError, match="plain host name"):
        create_app(make_runtime(), allowed_hosts=["*"])


# --- through a real server ---


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def serving(app: Any) -> Iterator[int]:
    """`app` under uvicorn with the WebSocket implementation the dashboard uses, on a free loopback port."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="websockets"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), name="test-api", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "the test API never started"
        time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def handshake_status(port: int, *, host: str, origin: str | None) -> int:
    """The HTTP status a WebSocket opening handshake gets: 101 when the stream opens."""
    lines = [
        "GET /api/events HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
        *([f"Origin: {origin}"] if origin is not None else []),
    ]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        reply = connection.recv(4096)
    return int(reply.split(b" ", 2)[1])


def test_a_real_server_refuses_foreign_websocket_handshakes_and_opens_loopback_ones(
    make_runtime: Callable[[], Runtime],
) -> None:
    with serving(create_app(make_runtime())) as port:
        local = f"127.0.0.1:{port}"
        assert handshake_status(port, host="evil.example", origin=None) == 400
        assert handshake_status(port, host=local, origin="https://evil.example") == 403
        assert handshake_status(port, host=local, origin="null") == 403
        assert handshake_status(port, host=local, origin=None) == 101
        assert handshake_status(port, host=local, origin="http://localhost:5173") == 101
        assert handshake_status(port, host=f"[::1]:{port}", origin=None) == 101
