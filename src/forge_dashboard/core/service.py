"""Self-describing services: typed config, state, actions, and events.

A service is a class with `id`, `title`, `description`, a pydantic `Config`
(persisted, editable), a pydantic `State` (pushed to clients as JSON patches
whenever it changes), `@action` methods taking one pydantic model and
returning one, and the `Event` types it publishes. The registry mounts every
service on the API from this description alone.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, get_type_hints

from pydantic import BaseModel, Field, ValidationError

from forge_dashboard.core.events import Event
from forge_dashboard.core.ui import ui

if TYPE_CHECKING:
    from forge_dashboard.core.runtime import Runtime


class ActionError(Exception):
    """A user-facing action failure; becomes an HTTP error with `message` as detail."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class NoInput(BaseModel):
    """Input of an action that takes no parameters."""


class Done(BaseModel):
    """Output of an action that only reports success."""

    ok: bool = True
    message: str | None = None


class ProjectRef(BaseModel):
    """Input naming one project; the UI fills it from the page it is on."""

    project_id: str = Field(json_schema_extra=ui(hidden=True, bind="project"))


class JobRef(BaseModel):
    """Handle to a long-running action; progress is reported by the jobs service."""

    job_id: str
    kind: str
    title: str


@dataclass(frozen=True)
class ActionMeta:
    title: str
    description: str | None
    long_running: bool
    danger: bool
    confirm: bool
    query: bool


ACTION_ATTRIBUTE = "__forge_action__"

ActionFn = Callable[..., Awaitable[BaseModel]]


def action[F: ActionFn](
    *,
    title: str,
    description: str | None = None,
    long_running: bool = False,
    danger: bool = False,
    confirm: bool = False,
    query: bool = False,
) -> Callable[[F], F]:
    """Mark a service method as an action.

    Args:
        title: Button label.
        description: Help text; defaults to the method docstring's first paragraph.
        long_running: The action returns a `JobRef` and reports through the jobs service.
        danger: Destructive styling and typed confirmation in the UI.
        confirm: Ask before running.
        query: A read-only request (the UI fetches with it but never shows it as a button).
    """

    def mark(fn: F) -> F:
        doc = inspect.getdoc(fn)
        summary = description if description is not None else (doc.split("\n\n", 1)[0] if doc else None)
        setattr(fn, ACTION_ATTRIBUTE, ActionMeta(title, summary, long_running, danger, confirm, query))
        return fn

    return mark


@dataclass(frozen=True)
class BoundAction:
    """An action resolved on a service instance, with its input and output models."""

    id: str
    meta: ActionMeta
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    call: Callable[[BaseModel], Awaitable[BaseModel]]


class StateSink(Protocol):
    def state_changed(self, service: Service[Any, Any]) -> None: ...


class Service[ConfigT: BaseModel, StateT: BaseModel]:
    """Base class for dashboard services."""

    id: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    events: ClassVar[tuple[type[Event], ...]] = ()

    Config: type[ConfigT]
    State: type[StateT]

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.config: ConfigT = self.Config()
        self.state: StateT = self.State()

    # --- lifecycle hooks ---

    async def load_config(self) -> None:
        """Read the persisted config; a stored config that no longer validates is reset loudly."""
        stored = await self.runtime.db.kv_get("config", self.id)
        if stored is None:
            return
        try:
            self.config = self.Config.model_validate(stored)
        except ValidationError as error:
            self.runtime.log.warning("config for %s no longer validates, using defaults: %s", self.id, error)

    async def start(self) -> None:
        """Begin background work. Called once, after every service has loaded its config."""

    async def stop(self) -> None:
        """End background work."""

    async def on_config_changed(self, previous: ConfigT) -> None:
        """React to an edited config."""

    # --- config and state ---

    async def update_config(self, config: ConfigT) -> ConfigT:
        previous = self.config
        self.config = config
        await self.runtime.db.kv_put("config", self.id, config.model_dump(mode="json"))
        await self.on_config_changed(previous)
        return self.config

    def set_state(self, state: StateT) -> None:
        self.state = state
        self.runtime.state_changed(self)

    def update_state(self, **changes: Any) -> None:
        """Replace top-level state fields, validating the result."""
        self.set_state(self.State.model_validate({**self.state.model_dump(), **changes}))

    def publish(self, event: Event) -> None:
        self.runtime.publish(self.id, event)

    # --- introspection ---

    @classmethod
    def action_specs(cls) -> list[tuple[str, ActionMeta, type[BaseModel], type[BaseModel]]]:
        """Every `@action` on the class with its input and output models.

        Raises:
            TypeError: when an action's signature is not `(self, input: Model) -> Model`.
        """
        specs: list[tuple[str, ActionMeta, type[BaseModel], type[BaseModel]]] = []
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
            meta = getattr(member, ACTION_ATTRIBUTE, None)
            if not isinstance(meta, ActionMeta):
                continue
            hints = get_type_hints(member)
            parameters = [p for p in inspect.signature(member).parameters.values() if p.name != "self"]
            if len(parameters) != 1:
                raise TypeError(f"{cls.__name__}.{name}: an action takes exactly one input model")
            input_model = hints.get(parameters[0].name)
            output_model = hints.get("return")
            for label, model in (("input", input_model), ("output", output_model)):
                if not (isinstance(model, type) and issubclass(model, BaseModel)):
                    raise TypeError(f"{cls.__name__}.{name}: {label} must be a pydantic model, got {model!r}")
            if meta.long_running and output_model is not JobRef:
                raise TypeError(f"{cls.__name__}.{name}: long-running actions return JobRef")
            assert isinstance(input_model, type) and isinstance(output_model, type)
            specs.append((name.replace("_", "-"), meta, input_model, output_model))
        return specs

    def bound_actions(self) -> list[BoundAction]:
        bound: list[BoundAction] = []
        for action_id, meta, input_model, output_model in self.action_specs():
            method = getattr(self, action_id.replace("-", "_"))
            bound.append(BoundAction(action_id, meta, input_model, output_model, method))
        return bound
