"""A service whose models cover every `x-ui` widget and display format.

It pins the schema renderer: the frontend's tests render this service's
descriptor (exported to `web/src/schema/__fixtures__/gallery.json` and
drift-tested), and the Settings page shows it as a live specimen.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import BaseModel, Field, JsonValue

from forge_dashboard.core.events import Event
from forge_dashboard.core.service import ActionError, Done, JobRef, NoInput, Service, action
from forge_dashboard.core.ui import ui
from forge_dashboard.services.jobs import JobContext, JobOutcome, JobsService

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime


Weekday = Literal["mon", "tue", "wed", "thu", "fri"]


def _default_days() -> list[Weekday]:
    return ["mon"]


class EmailTarget(BaseModel):
    kind: Literal["email"] = "email"
    address: str = Field("me@example.com", json_schema_extra=ui(widget="text", placeholder="name@host"))


class WebhookTarget(BaseModel):
    kind: Literal["webhook"] = "webhook"
    url: str = Field("https://example.com/hook", json_schema_extra=ui(widget="text"))
    secret: str = Field("", json_schema_extra=ui(widget="secret"))


class Endpoint(BaseModel):
    name: str = "local"
    port: int = Field(8080, ge=1, le=65535)
    enabled: bool = True


class GalleryConfig(BaseModel):
    """One field per widget kind."""

    title: str = Field("Specimen", json_schema_extra=ui(widget="text", group="Text", order=1, placeholder="A title"))
    notes: str = Field("", json_schema_extra=ui(widget="textarea", group="Text", order=2, help="Free-form notes."))
    count: int = Field(3, ge=0, json_schema_extra=ui(widget="number", group="Numbers", order=1, unit="items"))
    level: int = Field(5, ge=0, le=10, json_schema_extra=ui(widget="slider", group="Numbers", order=2, min=0, max=10))
    enabled: bool = Field(True, json_schema_extra=ui(widget="toggle", group="Choices", order=1))
    flavor: Literal["plain", "spicy", "sweet"] = Field(
        "plain",
        json_schema_extra=ui(
            widget="select",
            group="Choices",
            order=2,
            enum_labels={"plain": "Plain", "spicy": "Spicy", "sweet": "Sweet"},
        ),
    )
    strategy: Literal["merge", "rebase"] = Field(
        "merge",
        json_schema_extra=ui(
            widget="radio-cards",
            group="Choices",
            order=3,
            enum_labels={"merge": "Merge", "rebase": "Rebase"},
            enum_descriptions={"merge": "Keep history, add a merge commit.", "rebase": "Replay commits on top."},
        ),
    )
    days: list[Weekday] = Field(
        default_factory=_default_days, json_schema_extra=ui(widget="multiselect", group="Choices", order=4)
    )
    labels: list[str] = Field(default_factory=list, json_schema_extra=ui(widget="tags", group="Choices", order=5))
    folder: str = Field("", json_schema_extra=ui(widget="path-dir", group="Paths", order=1))
    file: str = Field("", json_schema_extra=ui(widget="path-file", group="Paths", order=2))
    accent: str = Field("#3b82f6", json_schema_extra=ui(widget="color", group="Appearance", order=1))
    timeout_s: int = Field(900, ge=0, json_schema_extra=ui(widget="duration", group="Timing", order=1))
    cron: str = Field("0 6 * * *", json_schema_extra=ui(widget="cron", group="Timing", order=2))
    token: str = Field("", json_schema_extra=ui(widget="secret", group="Secrets", order=1))
    script: str = Field("print('hi')", json_schema_extra=ui(widget="code", group="Advanced", order=1))
    extra: dict[str, JsonValue] = Field(default_factory=dict, json_schema_extra=ui(widget="json", group="Advanced"))
    model: str = Field("haiku", json_schema_extra=ui(widget="model-picker", group="Advanced"))
    verbose_level: int = Field(
        1, ge=0, le=3, json_schema_extra=ui(group="Advanced", visible_if={"field": "enabled", "equals": True})
    )
    hidden_marker: str = Field("internal", json_schema_extra=ui(hidden=True))
    tuning: float = Field(0.5, json_schema_extra=ui(advanced=True, help="Only for experts."))
    target: Annotated[EmailTarget | WebhookTarget, Field(discriminator="kind")] = Field(
        default_factory=EmailTarget, json_schema_extra=ui(group="Delivery")
    )
    endpoints: list[Endpoint] = Field(
        default_factory=lambda: [Endpoint()],
        json_schema_extra=ui(group="Delivery", columns=["name", "port", "enabled"]),
    )


class GalleryState(BaseModel):
    """One field per display format."""

    updated_at: float = Field(default_factory=time.time, json_schema_extra=ui(format="relative-time"))
    elapsed_s: float = Field(3725.0, json_schema_extra=ui(format="duration"))
    size: int = Field(5_368_709_120, json_schema_extra=ui(format="bytes"))
    ratio: float = Field(87.5, json_schema_extra=ui(format="percent"))
    progress: float = Field(0.6, json_schema_extra=ui(format="progress"))
    status: Literal["good", "warn", "failing"] = Field(
        "warn",
        json_schema_extra=ui(
            format="badge",
            enum_labels={"good": "good", "warn": "warn", "failing": "failing"},
            tones={"good": "good", "warn": "warn", "failing": "bad"},
        ),
    )
    commit: str = Field("1a2b3c4", json_schema_extra=ui(format="code"))
    link: str = Field("https://example.com", json_schema_extra=ui(format="url"))
    location: str = Field("C:/projects/demo", json_schema_extra=ui(format="path"))
    pings: int = 0


class EchoInput(BaseModel):
    message: str = Field(json_schema_extra=ui(widget="textarea", placeholder="Say something"))
    times: int = Field(1, ge=1, le=5, json_schema_extra=ui(widget="slider", min=1, max=5))
    shout: bool = False


class EchoOutput(BaseModel):
    echoed: list[str]


class Pinged(Event):
    type: ClassVar[str] = "gallery.pinged"
    count: int


class GalleryService(Service[GalleryConfig, GalleryState]):
    id = "gallery"
    title = "Schema gallery"
    description = "A specimen of every widget and display format the schema renderer supports."
    Config = GalleryConfig
    State = GalleryState
    events = (Pinged,)

    def __init__(self, runtime: Runtime, jobs: JobsService) -> None:
        super().__init__(runtime)
        self._jobs = jobs

    @action(title="Echo")
    async def echo(self, request: EchoInput) -> EchoOutput:
        """Repeat a message."""
        text = request.message.upper() if request.shout else request.message
        return EchoOutput(echoed=[text] * request.times)

    @action(title="Ping")
    async def ping(self, _request: NoInput) -> Done:
        """Bump the counter and publish an event."""
        count = self.state.pings + 1
        self.update_state(pings=count, updated_at=time.time())
        self.publish(Pinged(count=count))
        return Done(message=f"ping {count}")

    @action(title="Count slowly", long_running=True)
    async def count_slowly(self, _request: NoInput) -> JobRef:
        """A job that logs a few lines with progress."""

        async def work(context: JobContext) -> JobOutcome:
            for step in range(1, 4):
                context.log(f"step {step}")
                context.progress(step / 3, f"step {step} of 3")
                await asyncio.sleep(0.05)
            return JobOutcome(summary="counted to 3")

        return await self._jobs.submit(kind="gallery.count", title="Count slowly", work=work)

    @action(title="Fail", description="Always fails, to show error handling.")
    async def fail(self, _request: NoInput) -> Done:
        raise ActionError("this action always fails", status=409)

    @action(title="Reset counter", danger=True, confirm=True)
    async def reset(self, _request: NoInput) -> Done:
        """Set the ping counter back to zero."""
        self.update_state(pings=0)
        return Done(message="reset")
