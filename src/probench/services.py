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
    """声明一个服务操作的输入模型、读写性质及是否可暴露给 Agent。"""

    name: str
    description: str
    input_model: type[BaseModel]
    mutating: bool
    agent_visible: bool = True


class DomainAdapter(Protocol):
    """新领域服务需实现的契约；同一实现可绑定不同任务初态。

    `execute` 返回 `(工具结果, 状态变化)`。变化供后端历史与 Judge 使用，
    操作是否允许由任务权限和 OperationSpec 的 `mutating` 声明共同决定。
    """

    name: str
    state_model: type[BaseModel]
    operations: dict[str, OperationSpec]

    def is_mutating(self, operation: str) -> bool: ...
    def validate_state(self, state: dict[str, Any]) -> dict[str, Any]: ...
    def validate_arguments(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def execute(self, state: dict[str, Any], operation: str, arguments: dict[str, Any], *, mutate: bool) -> tuple[Any, dict[str, Any]]: ...


class ServiceBase:
    """为服务提供统一的 Pydantic 状态与参数验证。"""

    name: str
    state_model: type[BaseModel]
    operations: dict[str, OperationSpec]

    def is_mutating(self, operation: str) -> bool:
        return self.operations[operation].mutating

    def validate_state(self, state: dict[str, Any]) -> dict[str, Any]:
        # RootModel（如 KV）与普通 BaseModel 的导出形状不同。
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


class RecordState(RootModel[dict[str, dict[str, Any]]]):
    """A service-local collection keyed by stable record IDs."""


class RecordIdArgs(BaseModel):
    id: str


class RecordSearchArgs(BaseModel):
    query: str = Field(min_length=1)


class RecordPatchArgs(BaseModel):
    id: str
    changes: dict[str, Any]


class RecordInsertArgs(BaseModel):
    record: dict[str, Any]


class RecordAdapter(ServiceBase):
    """Small reusable record store for task data, without domain-specific policy."""

    name = "records"
    state_model = RecordState
    operations = {
        "list": OperationSpec("list", "List record summaries in this service.", EmptyArgs, False),
        "search": OperationSpec("search", "Search this service's records by text.", RecordSearchArgs, False),
        "get": OperationSpec("get", "Read a record by its ID.", RecordIdArgs, False),
        "patch": OperationSpec("patch", "Update fields on an existing record.", RecordPatchArgs, True),
        "create": OperationSpec("create", "Create a new service record.", RecordInsertArgs, True),
        "insert": OperationSpec("insert", "Insert a new external record (world controller only).", RecordInsertArgs, True, agent_visible=False),
    }

    def __init__(self, config: dict[str, Any] | None = None):
        if config:
            raise ValueError("records service has no configuration options")

    def validate_state(self, state: dict[str, Any]) -> dict[str, Any]:
        normalized = super().validate_state(state)
        for record_id, record in normalized.items():
            if record.get("id") != record_id:
                raise ValueError(f"record ID mismatch: {record_id}")
        return normalized

    @staticmethod
    def _summary(record: dict[str, Any]) -> dict[str, Any]:
        fields = ("id", "kind", "subject", "title", "name", "code", "status", "sent_at", "start_at", "updated_at")
        return {key: record[key] for key in fields if key in record}

    def execute(self, state, operation, arguments, *, mutate):
        if operation == "list":
            return [self._summary(record) for record in state.values()], {}
        if operation == "search":
            query = arguments["query"].casefold()
            return [self._summary(record) for record in state.values() if query in json.dumps(record, ensure_ascii=False, default=str).casefold()], {}
        if operation == "get":
            return copy.deepcopy(state.get(arguments["id"])), {}
        if operation == "patch" and mutate:
            record_id, changes = arguments["id"], arguments["changes"]
            if record_id not in state:
                raise ValueError(f"record does not exist: {record_id}")
            if "id" in changes:
                raise ValueError("record ID is immutable")
            before = copy.deepcopy(state[record_id])
            state[record_id].update(copy.deepcopy(changes))
            return {"before": before, "after": copy.deepcopy(state[record_id])}, {record_id: copy.deepcopy(changes)}
        if operation in ("create", "insert") and mutate:
            record = copy.deepcopy(arguments["record"])
            record_id = record.get("id")
            if not isinstance(record_id, str) or not record_id or record_id in state:
                raise ValueError(f"invalid or existing record ID: {record_id}")
            state[record_id] = record
            return {"inserted_id": record_id}, {record_id: record}
        raise ValueError(f"unsupported or unauthorized operation: {operation}")


class ServiceRegistry:
    """保存服务工厂；任务选择服务名，而非复制服务实现。"""

    def __init__(self):
        self._factories: dict[str, Callable[[dict[str, Any]], DomainAdapter]] = {}

    def register(self, name: str, factory: Callable[[dict[str, Any]], DomainAdapter]):
        if name in self._factories:
            raise ValueError(f"service already registered: {name}")
        self._factories[name] = factory

    def create(self, name: str, config: dict[str, Any] | None = None) -> DomainAdapter:
        # 每个任务绑定都创建独立适配器实例，配置由任务清单提供。
        if name not in self._factories:
            raise ValueError(f"unknown service: {name}")
        return self._factories[name](config or {})

    def names(self) -> list[str]:
        return sorted(self._factories)


DEFAULT_REGISTRY = ServiceRegistry()
DEFAULT_REGISTRY.register("kv", KVAdapter)
DEFAULT_REGISTRY.register("counter", CounterAdapter)
DEFAULT_REGISTRY.register("records", RecordAdapter)


def load_service_modules():
    """Import optional modules that register factories in DEFAULT_REGISTRY."""
    for module in os.environ.get("BENCH_SERVICE_MODULES", "").split(","):
        if module.strip():
            importlib.import_module(module.strip())
