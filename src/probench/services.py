"""Reusable service contracts and the registry used by tasks and MCP."""
from __future__ import annotations

import copy
import importlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field, RootModel


class EmptyArgs(BaseModel):
    pass


@dataclass(frozen=True)
class OperationSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    mutating: bool
    agent_visible: bool = True


class DomainAdapter(Protocol):
    name: str
    state_model: type[BaseModel]
    operations: dict[str, OperationSpec]

    def is_mutating(self, operation: str) -> bool: ...
    def validate_state(self, state: dict[str, Any]) -> dict[str, Any]: ...
    def validate_arguments(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def execute(self, state: dict[str, Any], operation: str, arguments: dict[str, Any], *, mutate: bool) -> tuple[Any, dict[str, Any]]: ...


class ServiceBase:
    name: str
    state_model: type[BaseModel]
    operations: dict[str, OperationSpec]

    def is_mutating(self, operation: str) -> bool:
        return self.operations[operation].mutating

    def validate_state(self, state: dict[str, Any]) -> dict[str, Any]:
        parsed = self.state_model.model_validate(state)
        normalized = parsed.model_dump()
        if isinstance(parsed, RootModel):
            normalized = parsed.root
        json.dumps(normalized)
        return normalized

    def validate_arguments(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation not in self.operations:
            raise ValueError(f"unsupported {self.name} operation: {operation}")
        normalized = self.operations[operation].input_model.model_validate(arguments).model_dump()
        json.dumps(normalized)
        return normalized


class KVState(RootModel[dict[str, Any]]):
    pass


class GetArgs(BaseModel):
    key: str


class SetArgs(BaseModel):
    key: str
    value: int | str | bool | list[Any] | dict[str, Any] | None


class KVAdapter(ServiceBase):
    """General key/value service kept for the original synthetic fixture."""
    name = "kv"
    state_model = KVState
    operations = {
        "get": OperationSpec("get", "Read one key from the simulated world.", GetArgs, False),
        "set": OperationSpec("set", "Write one key in the simulated world.", SetArgs, True),
    }

    def __init__(self, config: dict[str, Any] | None = None):
        if config:
            raise ValueError("kv service has no configuration options")

    def execute(self, state, operation, arguments, *, mutate):
        key = arguments["key"]
        if operation == "get":
            return copy.deepcopy(state.get(key)), {}
        if operation == "set" and mutate:
            before = copy.deepcopy(state.get(key))
            state[key] = copy.deepcopy(arguments["value"])
            return {"before": before, "after": state[key]}, {key: state[key]}
        raise ValueError(f"unsupported or unauthorized operation: {operation}")


class CounterState(BaseModel):
    value: int = Field(ge=0)


class IncrementArgs(BaseModel):
    amount: int = Field(gt=0)


class CounterAdapter(ServiceBase):
    """Small reusable synthetic service; the same class backs multiple tasks."""
    name = "counter"
    state_model = CounterState
    operations = {
        "read": OperationSpec("read", "Read the synthetic counter value.", EmptyArgs, False),
        "increment": OperationSpec("increment", "Increment the synthetic counter by a positive amount.", IncrementArgs, True),
    }

    def __init__(self, config: dict[str, Any] | None = None):
        if config:
            raise ValueError("counter service has no configuration options")

    def execute(self, state, operation, arguments, *, mutate):
        if operation == "read":
            return state["value"], {}
        if operation == "increment" and mutate:
            before = state["value"]
            state["value"] += arguments["amount"]
            return {"before": before, "after": state["value"]}, {"value": state["value"]}
        raise ValueError(f"unsupported or unauthorized operation: {operation}")


class ServiceRegistry:
    def __init__(self):
        self._factories: dict[str, Callable[[dict[str, Any]], DomainAdapter]] = {}

    def register(self, name: str, factory: Callable[[dict[str, Any]], DomainAdapter]):
        if name in self._factories:
            raise ValueError(f"service already registered: {name}")
        self._factories[name] = factory

    def create(self, name: str, config: dict[str, Any] | None = None) -> DomainAdapter:
        if name not in self._factories:
            raise ValueError(f"unknown service: {name}")
        return self._factories[name](config or {})

    def names(self) -> list[str]:
        return sorted(self._factories)


DEFAULT_REGISTRY = ServiceRegistry()
DEFAULT_REGISTRY.register("kv", KVAdapter)
DEFAULT_REGISTRY.register("counter", CounterAdapter)


def load_service_modules():
    """Import optional modules that register factories in DEFAULT_REGISTRY."""
    for module in os.environ.get("BENCH_SERVICE_MODULES", "").split(","):
        if module.strip():
            importlib.import_module(module.strip())
