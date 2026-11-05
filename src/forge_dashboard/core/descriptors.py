"""`ServiceDescriptor`: the self-description `GET /api/services` returns."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, JsonValue

from forge_dashboard.core.service import Service


class ActionDescriptor(BaseModel):
    id: str
    title: str
    description: str | None
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    long_running: bool
    danger: bool
    confirm: bool
    query: bool


class EventDescriptor(BaseModel):
    type: str
    data_schema: dict[str, JsonValue] = Field(serialization_alias="schema")


class ServiceDescriptor(BaseModel):
    id: str
    title: str
    description: str
    config_schema: dict[str, JsonValue]
    state_schema: dict[str, JsonValue]
    actions: list[ActionDescriptor]
    events: list[EventDescriptor]


def _schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    schema: dict[str, Any] = model.model_json_schema(mode="serialization")
    return schema


def describe(service: Service[Any, Any]) -> ServiceDescriptor:
    return ServiceDescriptor(
        id=service.id,
        title=service.title,
        description=service.description,
        config_schema=_schema(service.Config),
        state_schema=_schema(service.State),
        actions=[
            ActionDescriptor(
                id=action_id,
                title=meta.title,
                description=meta.description,
                input_schema=input_model.model_json_schema(mode="validation"),
                output_schema=_schema(output_model),
                long_running=meta.long_running,
                danger=meta.danger,
                confirm=meta.confirm,
                query=meta.query,
            )
            for action_id, meta, input_model, output_model in service.action_specs()
        ],
        events=[EventDescriptor(type=event.type, data_schema=_schema(event)) for event in service.events],
    )
