"""The API server entry point: what it binds, whom it serves, and what it tells the CLI."""

import asyncio
import json
import socket
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from test_cli import seed_discovery

from forge_dashboard import server
from forge_dashboard.core.paths import DataPaths


@pytest.mark.parametrize(
    ("host", "advertised"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("localhost", "localhost"),
        ("0.0.0.0", "127.0.0.1"),
        ("::", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("::1", "[::1]"),
        ("192.168.1.5", "192.168.1.5"),
    ],
)
def test_the_server_file_advertises_an_address_a_client_on_this_computer_can_use(host: str, advertised: str) -> None:
    url, _hosts = server.server_endpoint(host, 8760, [])

    assert url == f"http://{advertised}:8760"


def test_the_served_hosts_are_loopback_the_allowed_names_and_the_advertised_address() -> None:
    _url, hosts = server.server_endpoint("192.168.1.5", 8760, ["dashboard.lan"])

    assert hosts == {"127.0.0.1", "localhost", "::1", "dashboard.lan", "192.168.1.5"}
    assert server.server_endpoint("0.0.0.0", 8760, [])[1] == {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
def serve_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, Sequence[str]]]:
    calls: list[tuple[str, int, Sequence[str]]] = []

    async def record(host: str, port: int, _static: Path | None, allow_hosts: Sequence[str] = ()) -> None:
        calls.append((host, port, allow_hosts))

    monkeypatch.setattr(server, "serve", record)
    return calls


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "dashboard.lan"])
def test_binding_beyond_loopback_is_refused_without_an_allowed_host(
    host: str, serve_calls: list[tuple[str, int, Sequence[str]]], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        server.main(["--port", "8760", "--host", host])

    assert refused.value.code == 2
    assert "--allow-host" in capsys.readouterr().err
    assert serve_calls == []


def test_binding_beyond_loopback_is_served_once_hosts_are_allowed(
    serve_calls: list[tuple[str, int, Sequence[str]]],
) -> None:
    args = ["--port", "8760", "--host", "0.0.0.0", "--allow-host", "Dashboard.LAN", "--allow-host", "10.0.0.5"]

    assert server.main(args) == 0

    assert serve_calls == [("0.0.0.0", 8760, ["dashboard.lan", "10.0.0.5"])]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_needs_no_opt_in(host: str, serve_calls: list[tuple[str, int, Sequence[str]]]) -> None:
    assert server.main(["--port", "8760", "--host", host]) == 0

    assert serve_calls == [(host, 8760, [])]


@pytest.mark.parametrize("name", ["*", "dashboard.lan:8760", "http://dashboard.lan"])
def test_an_allowed_host_must_be_one_plain_name(
    name: str, serve_calls: list[tuple[str, int, Sequence[str]]], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        server.main(["--port", "8760", "--allow-host", name])

    assert refused.value.code == 2
    assert "plain host name" in capsys.readouterr().err
    assert serve_calls == []


@pytest.fixture
def paths_without_projects(tmp_path: Path) -> DataPaths:
    paths = DataPaths.default()
    seed_discovery(paths, tmp_path / "no-projects")
    return paths


async def test_serving_tells_the_cli_only_where_to_reach_it_and_forgets_it_on_exit(
    paths_without_projects: DataPaths,
) -> None:
    paths = paths_without_projects
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    url = f"http://127.0.0.1:{port}"
    serving = asyncio.create_task(server.serve("127.0.0.1", port, None))
    try:
        async with httpx.AsyncClient(base_url=url, timeout=5) as client:
            for _ in range(600):
                try:
                    if (await client.get("/api/health")).is_success:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("the server never became healthy")
            assert json.loads(paths.server_file.read_text(encoding="utf-8")) == {"url": url}
            rebound = await client.get("/api/health", headers={"Host": "evil.example"})
            assert rebound.status_code == 400
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)

    assert not paths.server_file.exists()
