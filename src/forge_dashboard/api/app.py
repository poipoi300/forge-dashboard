"""FastAPI app factory: every registered service mounted from its self-description.

Routes per service are generated, so the OpenAPI document (and the TypeScript
types generated from it) carries every concrete model:

- `GET  /api/services`                         all `ServiceDescriptor`s
- `GET  /api/services/{id}/config`             typed config
- `PUT  /api/services/{id}/config`             validate, persist, apply
- `GET  /api/services/{id}/state`              typed state at a revision
- `POST /api/services/{id}/actions/{action}`   typed input -> typed output
- `WS   /api/events`                           state patches and events, multiplexed
- `GET  /api/health`                           200 once every service started
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import anyio
from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, create_model
from pydantic.json_schema import models_json_schema

from forge_dashboard.core.descriptors import ServiceDescriptor, describe
from forge_dashboard.core.events import EventEnvelope
from forge_dashboard.core.runtime import Runtime, StatePatch, StateSnapshot
from forge_dashboard.core.service import ActionError, BoundAction, Service


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    services: int


class HelloMessage(BaseModel):
    kind: Literal["hello"] = "hello"
    states: list[StateSnapshot]


class StateMessage(BaseModel):
    kind: Literal["state"] = "state"
    patch: StatePatch


class EventMessage(BaseModel):
    kind: Literal["event"] = "event"
    event: EventEnvelope


# Models that only travel over the WebSocket; added to the OpenAPI components
# so the generated TypeScript types cover the stream too.
STREAM_MODELS: tuple[type[BaseModel], ...] = (HelloMessage, StateMessage, EventMessage)

RouteInstaller = Callable[[APIRouter, Runtime], None]

_SCHEMA_REF = "#/components/schemas/"


def _refs(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_SCHEMA_REF):
            yield ref.removeprefix(_SCHEMA_REF)
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def _closure(schemas: dict[str, Any], roots: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        pending.extend(_refs(schemas[name]))
    return seen


def require_output_fields(document: dict[str, Any], *, extra_outputs: Iterable[str]) -> None:
    """Mark every field required on schemas that only ever describe server output.

    pydantic serializes every field, defaulted or not, so a response always
    carries them; its serialization-mode JSON Schema still leaves defaulted
    fields optional, which would make generated clients guard values that
    are always present. Schemas also reachable from a request body or
    parameter keep their optional fields, because clients may omit those.
    """
    schemas: dict[str, Any] = document.get("components", {}).get("schemas", {})
    inputs: list[str] = []
    outputs: list[str] = list(extra_outputs)
    for operations in document.get("paths", {}).values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            inputs.extend(_refs(operation.get("requestBody", {})))
            inputs.extend(_refs(operation.get("parameters", [])))
            outputs.extend(_refs(operation.get("responses", {})))
    for name in _closure(schemas, outputs) - _closure(schemas, inputs):
        schema = schemas[name]
        if isinstance(schema.get("properties"), dict):
            schema["required"] = list(schema["properties"])


def _typed_endpoint(
    parameter: str, annotation: type[BaseModel] | None, returns: type[BaseModel], call: Callable[..., Awaitable[Any]]
) -> Callable[..., Awaitable[Any]]:
    """Give a generated endpoint a real signature, which is what FastAPI reads for body and response models."""

    async def endpoint(**kwargs: Any) -> Any:
        return await call(*kwargs.values())

    parameters = (
        [inspect.Parameter(parameter, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)] if annotation else []
    )
    endpoint.__signature__ = inspect.Signature(parameters, return_annotation=returns)  # type: ignore[attr-defined]
    return endpoint


def _mount_service(router: APIRouter, service: Service[Any, Any]) -> None:
    base = f"/api/services/{service.id}"
    snapshot_model = create_model(
        f"{service.State.__name__}Snapshot", __base__=BaseModel, rev=(int, ...), state=(service.State, ...)
    )

    async def get_config() -> BaseModel:
        current: BaseModel = service.config
        return current

    async def put_config(config: BaseModel) -> BaseModel:
        updated: BaseModel = await service.update_config(config)
        return updated

    async def get_state() -> BaseModel:
        return snapshot_model(rev=service.runtime.states.snapshot(service.id).rev, state=service.state)

    routes: list[tuple[str, str, Callable[..., Awaitable[Any]], type[BaseModel], str]] = [
        ("GET", "/config", _typed_endpoint("config", None, service.Config, get_config), service.Config, "get_config"),
        (
            "PUT",
            "/config",
            _typed_endpoint("config", service.Config, service.Config, put_config),
            service.Config,
            "put_config",
        ),
        ("GET", "/state", _typed_endpoint("state", None, snapshot_model, get_state), snapshot_model, "get_state"),
    ]
    for method, path, endpoint, response_model, operation in routes:
        router.add_api_route(
            f"{base}{path}",
            endpoint,
            methods=[method],
            response_model=response_model,
            operation_id=f"{service.id}_{operation}",
            tags=[service.id],
        )
    for bound in service.bound_actions():
        _mount_action(router, base, service.id, bound)


def _mount_action(router: APIRouter, base: str, service_id: str, bound: BoundAction) -> None:
    async def invoke(request: BaseModel) -> BaseModel:
        return await bound.call(request)

    router.add_api_route(
        f"{base}/actions/{bound.id}",
        _typed_endpoint("request", bound.input_model, bound.output_model, invoke),
        methods=["POST"],
        response_model=bound.output_model,
        operation_id=f"{service_id}_{bound.id.replace('-', '_')}",
        summary=bound.meta.title,
        description=bound.meta.description or "",
        tags=[service_id],
    )


async def _stream(websocket: WebSocket, runtime: Runtime) -> None:
    """Send a hello with every snapshot, then patches and events as they happen."""
    await websocket.accept()
    async with runtime.states.subscribe() as patches, runtime.bus.subscribe() as events:
        await websocket.send_text(HelloMessage(states=runtime.states.snapshots()).model_dump_json())

        async def forward_patches() -> None:
            while True:
                patch = await patches.get()
                message = (
                    HelloMessage(states=runtime.states.snapshots()) if patch is None else StateMessage(patch=patch)
                )
                await websocket.send_text(message.model_dump_json())

        async def forward_events() -> None:
            while True:
                envelope = await events.get()
                await websocket.send_text(EventMessage(event=envelope).model_dump_json())

        # Structured with anyio rather than raw asyncio tasks: the ASGI server
        # cancels a connection through anyio cancel scopes, which propagate
        # cleanly only into anyio-managed children.
        async with anyio.create_task_group() as group:

            async def receive() -> None:
                try:
                    while True:
                        message = await websocket.receive_json()
                        if isinstance(message, dict) and message.get("kind") == "resync":
                            hello = HelloMessage(states=runtime.states.snapshots())
                            await websocket.send_text(hello.model_dump_json())
                except WebSocketDisconnect:
                    group.cancel_scope.cancel()

            group.start_soon(forward_patches)
            group.start_soon(forward_events)
            group.start_soon(receive)


def _spa(static_dir: Path) -> Callable[[str], Awaitable[FileResponse]]:
    root = static_dir.resolve()
    index = root / "index.html"

    async def serve(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)

    return serve


def create_app(
    runtime: Runtime,
    *,
    static_dir: Path | None = None,
    route_installers: Iterable[RouteInstaller] = (),
    on_ready: Callable[[Runtime], Awaitable[None]] | None = None,
) -> FastAPI:
    """Mount `runtime`'s services on a FastAPI app; storage opens and services start in the lifespan.

    Args:
        runtime: A runtime with every service already registered.
        static_dir: Built UI served at `/` with an SPA fallback, when present.
        route_installers: Non-service routes (scripting API, report export).
        on_ready: Called after every service started (writes the server file).
    """
    ready = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        if on_ready is not None:
            await on_ready(runtime)
        ready.set()
        try:
            yield
        finally:
            ready.clear()
            await runtime.stop()

    app = FastAPI(title="forge dashboard", version="1", lifespan=lifespan)

    @app.exception_handler(ActionError)
    async def action_error(_request: Request, error: ActionError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"detail": error.message})

    @app.get("/api/health", operation_id="health")
    async def health() -> Health:
        if not ready.is_set():
            raise HTTPException(status_code=503, detail="starting")
        return Health(services=len(runtime.services))

    @app.get("/api/services", operation_id="list_services")
    async def services() -> list[ServiceDescriptor]:
        return [describe(service) for service in runtime.services.values()]

    @app.websocket("/api/events")
    async def events(websocket: WebSocket) -> None:
        with contextlib.suppress(WebSocketDisconnect):
            await _stream(websocket, runtime)

    router = APIRouter()
    for service in runtime.services.values():
        _mount_service(router, service)
    for install in route_installers:
        install(router, runtime)
    app.include_router(router)

    if static_dir is not None and (static_dir / "index.html").is_file():
        assets = static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")
        app.add_api_route("/{path:path}", _spa(static_dir), methods=["GET"], include_in_schema=False)

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            document = get_openapi(title=app.title, version=app.version, routes=app.routes)
            _keys, extra = models_json_schema(
                [(model, "serialization") for model in STREAM_MODELS], ref_template="#/components/schemas/{model}"
            )
            document.setdefault("components", {}).setdefault("schemas", {}).update(extra.get("$defs", {}))
            require_output_fields(document, extra_outputs=[model.__name__ for model in STREAM_MODELS])
            app.openapi_schema = document
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
