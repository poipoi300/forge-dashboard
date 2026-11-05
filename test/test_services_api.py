"""The service registry mounts self-describing services on the API and the event stream."""

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from forge_dashboard.api.app import create_app
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import JobRef, NoInput, Service, action

ALL_WIDGETS = {
    "text",
    "textarea",
    "number",
    "slider",
    "toggle",
    "select",
    "radio-cards",
    "multiselect",
    "tags",
    "path-dir",
    "path-file",
    "color",
    "duration",
    "cron",
    "secret",
    "code",
    "json",
    "model-picker",
}
ALL_FORMATS = {"relative-time", "duration", "bytes", "percent", "progress", "badge", "code", "url", "path"}


def _hints(schema: dict[str, Any], key: str) -> set[str]:
    return {prop["x-ui"][key] for prop in schema["properties"].values() if key in prop.get("x-ui", {})}


@pytest.fixture
def client(make_runtime: Callable[[], Runtime]) -> Any:
    with TestClient(create_app(make_runtime())) as test_client:
        yield test_client


def test_health_is_ok_once_started(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "services": 3}


def test_descriptors_cover_every_widget_and_format(client: TestClient) -> None:
    descriptors = {d["id"]: d for d in client.get("/api/services").json()}
    gallery = descriptors["gallery"]
    assert _hints(gallery["config_schema"], "widget") == ALL_WIDGETS
    assert _hints(gallery["state_schema"], "format") == ALL_FORMATS
    actions = {a["id"]: a for a in gallery["actions"]}
    assert actions["count-slowly"]["long_running"] is True
    assert actions["reset"]["danger"] is True and actions["reset"]["confirm"] is True
    assert actions["echo"]["input_schema"]["properties"]["message"]["x-ui"]["widget"] == "textarea"
    assert [event["type"] for event in gallery["events"]] == ["gallery.pinged"]
    assert {a["id"] for a in descriptors["jobs"]["actions"] if a["query"]} == {"get", "log", "history"}


def test_actions_validate_input_and_return_typed_output(client: TestClient) -> None:
    response = client.post("/api/services/gallery/actions/echo", json={"message": "hi", "times": 2, "shout": True})
    assert response.json() == {"echoed": ["HI", "HI"]}
    invalid = client.post("/api/services/gallery/actions/echo", json={"times": 9})
    assert invalid.status_code == 422


def test_action_errors_become_http_errors(client: TestClient) -> None:
    response = client.post("/api/services/gallery/actions/fail", json={})
    assert response.status_code == 409
    assert response.json() == {"detail": "this action always fails"}


def test_config_round_trips_and_persists(client: TestClient) -> None:
    config = client.get("/api/services/gallery/config").json()
    config["title"] = "Renamed"
    assert client.put("/api/services/gallery/config", json=config).json()["title"] == "Renamed"
    assert client.get("/api/services/gallery/config").json()["title"] == "Renamed"
    config["level"] = 99
    assert client.put("/api/services/gallery/config", json=config).status_code == 422


def test_config_survives_a_restart(make_runtime: Callable[[], Runtime]) -> None:
    with TestClient(create_app(make_runtime())) as first:
        config = first.get("/api/services/gallery/config").json()
        first.put("/api/services/gallery/config", json={**config, "count": 7})
    with TestClient(create_app(make_runtime())) as second:
        assert second.get("/api/services/gallery/config").json()["count"] == 7


def test_event_stream_sends_snapshots_then_patches_and_events(client: TestClient) -> None:
    with client.websocket_connect("/api/events") as stream:
        hello = stream.receive_json()
        assert hello["kind"] == "hello"
        states = {snapshot["service"]: snapshot for snapshot in hello["states"]}
        assert states["gallery"]["state"]["pings"] == 0
        client.post("/api/services/gallery/actions/ping", json={})
        received = [stream.receive_json() for _ in range(2)]
        kinds = {message["kind"]: message for message in received}
        assert kinds["event"]["event"]["type"] == "gallery.pinged"
        patch = kinds["state"]["patch"]
        assert patch["service"] == "gallery" and patch["rev"] == 1
        assert {"op": "replace", "path": "/pings", "value": 1} in patch["ops"]
        stream.send_json({"kind": "resync"})
        assert stream.receive_json()["kind"] == "hello"


def test_state_endpoint_reports_revision(client: TestClient) -> None:
    client.post("/api/services/gallery/actions/ping", json={})
    time.sleep(0.2)
    body = client.get("/api/services/gallery/state").json()
    assert body["state"]["pings"] == 1
    assert body["rev"] >= 1


def test_long_running_actions_return_job_refs_tracked_by_jobs(client: TestClient) -> None:
    ref = client.post("/api/services/gallery/actions/count-slowly", json={}).json()
    assert set(ref) == {"job_id", "kind", "title"}
    deadline = time.time() + 10
    while time.time() < deadline:
        detail = client.post("/api/services/jobs/actions/get", json={"job_id": ref["job_id"]}).json()
        if detail["job"]["status"] == "succeeded":
            break
        time.sleep(0.05)
    assert detail["job"]["summary"] == "counted to 3"
    log = client.post("/api/services/jobs/actions/log", json={"job_id": ref["job_id"]}).json()
    assert log["lines"] == ["step 1", "step 2", "step 3"] and log["done"] is True


def test_openapi_carries_every_action_and_the_stream_models(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    assert "/api/services/gallery/actions/echo" in document["paths"]
    assert {"HelloMessage", "StateMessage", "EventMessage", "EchoInput", "GalleryConfig"} <= set(
        document["components"]["schemas"]
    )


class _Broken(Service[NoInput, NoInput]):
    id = "broken"
    title = "Broken"
    description = "Invalid action signatures."
    Config = NoInput
    State = NoInput


def test_actions_must_take_and_return_models() -> None:
    class TwoInputs(_Broken):
        @action(title="Bad")
        async def bad(self, first: NoInput, second: NoInput) -> NoInput:
            return first

    class NotAModel(_Broken):
        @action(title="Bad")
        async def bad(self, value: int) -> NoInput:
            return NoInput()

    class LongWithoutJob(_Broken):
        @action(title="Bad", long_running=True)
        async def bad(self, _request: NoInput) -> NoInput:
            return NoInput()

    for broken, message in ((TwoInputs, "exactly one"), (NotAModel, "pydantic model"), (LongWithoutJob, "JobRef")):
        with pytest.raises(TypeError, match=message):
            broken.action_specs()


def test_action_ids_are_kebab_case_and_descriptions_default_to_docstrings() -> None:
    class Documented(_Broken):
        @action(title="Do it")
        async def do_the_thing(self, _request: NoInput) -> JobRef:
            """Does the thing.

            Details nobody needs in a tooltip.
            """
            return JobRef(job_id="x", kind="k", title="t")

    [(action_id, meta, _input, output)] = Documented.action_specs()
    assert action_id == "do-the-thing"
    assert meta.description == "Does the thing."
    assert issubclass(output, BaseModel)
