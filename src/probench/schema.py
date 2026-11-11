from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from .services import DEFAULT_REGISTRY, DomainAdapter, ServiceRegistry, load_service_modules
from .judge_evidence import EvidenceSources


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require a timezone offset")
    return value


class ServiceBinding(BaseModel):
    id: str
    service: str
    config: dict[str, Any] = Field(default_factory=dict)
    tool_prefix: str | None = None

    @model_validator(mode="after")
    def valid_names(self):
        for name in (self.id, self.service, self.tool_prefix or self.id):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid service or tool name: {name}")
        return self

    def mcp_tool_name(self, operation: str) -> str:
        return f"{self.tool_prefix or self.id}_{operation}"


class Manifest(BaseModel):
    task_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    initial_timestamp: datetime
    duration_seconds: int = Field(ge=0)
    adapters: list[str] = Field(default_factory=lambda: ["kv"])
    services: list[ServiceBinding] | None = None
    workspace_files: dict[str, str] = Field(default_factory=dict)
    tool_permissions: dict[str, list[str]] = Field(default_factory=dict)
    judge: dict[str, Any] = Field(default_factory=dict)
    initial_world: str = "initial_world.yaml"
    events: str = "events.yaml"
    context: str = "context.yaml"
    notification_rules: str = "judge/notification_rules.yaml"
    judge_instructions: str | None = None
    evidence_sources: str | None = None

    _timestamp = field_validator("initial_timestamp")(aware)

    def bindings(self) -> list[ServiceBinding]:
        if self.services is not None:
            return self.services
        return [ServiceBinding(id=name, service=name, tool_prefix="world" if name == "kv" else name) for name in self.adapters]


class Event(BaseModel):
    id: str
    at: datetime
    kind: Literal["world_mutation", "user_message", "notification", "scheduled_wakeup", "time_advance"]
    adapter: str = "kv"
    operation: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    body: str | None = None

    _timestamp = field_validator("at")(aware)

    @model_validator(mode="after")
    def check_mutation(self):
        if self.kind == "world_mutation" and not self.operation:
            raise ValueError("world_mutation requires operation")
        if self.kind in {"user_message", "notification"} and not self.body:
            raise ValueError(f"{self.kind} requires body")
        return self


class ContextDisclosure(BaseModel):
    event_id: str
    goals: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    interaction: str | None = None


class NotificationRule(BaseModel):
    id: str
    description: str
    trigger_event_ids: list[str] = Field(default_factory=list)
    deadline_seconds: int = Field(ge=0)
    related_goal: str
    content_requirements: list[str] = Field(default_factory=list)
    closing_conditions: str | None = None
    unnecessary_conditions: str | None = None
    authorization_requirements: str | None = None


class TaskPackage(BaseModel):
    _registry: ServiceRegistry = PrivateAttr(default=DEFAULT_REGISTRY)
    root: Path
    manifest: Manifest
    initial_world: dict[str, Any]
    events: list[Event]
    context: list[ContextDisclosure]
    rules: list[NotificationRule]
    judge_instructions: str = ""
    judge_evidence_sources: EvidenceSources = Field(default_factory=EvidenceSources)

    def service_adapters(self) -> dict[str, DomainAdapter]:
        return {binding.id: self._registry.create(binding.service, binding.config) for binding in self.manifest.bindings()}

    def goal_context(self, disclosed_event_ids: set[str]) -> dict[str, Any]:
        event_order = {event.id: index for index, event in enumerate(self.events)}
        rows = [c.model_dump() for c in sorted(self.context, key=lambda row: event_order[row.event_id]) if c.event_id in disclosed_event_ids]
        return {"disclosures": rows}


def _read(root: Path, relative: str) -> Any:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes task root: {relative}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _read_text(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if not path.is_relative_to((root / "judge").resolve()):
        raise ValueError(f"Judge file must be inside task judge directory: {relative}")
    return path.read_text(encoding="utf-8")


def _read_judge_yaml(root: Path, relative: str) -> Any:
    path = (root / relative).resolve()
    if not path.is_relative_to((root / "judge").resolve()):
        raise ValueError(f"Judge file must be inside task judge directory: {relative}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_task(path: str | Path, registry: ServiceRegistry = DEFAULT_REGISTRY) -> TaskPackage:
    if registry is DEFAULT_REGISTRY:
        load_service_modules()
    root = Path(path).resolve()
    manifest = Manifest.model_validate(_read(root, "manifest.yaml"))
    world = _read(root, manifest.initial_world) or {}
    events = [Event.model_validate(x) for x in (_read(root, manifest.events) or [])]
    contexts = [ContextDisclosure.model_validate(x) for x in (_read(root, manifest.context) or [])]
    rules = [NotificationRule.model_validate(x) for x in (_read(root, manifest.notification_rules) or [])]
    instructions = _read_text(root, manifest.judge_instructions) if manifest.judge_instructions else ""
    evidence_sources = EvidenceSources.model_validate(_read_judge_yaml(root, manifest.evidence_sources)) if manifest.evidence_sources else EvidenceSources()
    ids = [e.id for e in events]
    if len(set(ids)) != len(ids) or events != sorted(events, key=lambda e: e.at):
        raise ValueError("event IDs must be unique and events sorted by timestamp")
    event_by_id = {e.id: e for e in events}
    if any(c.event_id not in event_by_id or event_by_id[c.event_id].kind != "user_message" for c in contexts):
        raise ValueError("context disclosures must reference user_message events")
    if any(x not in event_by_id for rule in rules for x in rule.trigger_event_ids):
        raise ValueError("notification rule references unknown event")
    if any(e.at < manifest.initial_timestamp or (e.at - manifest.initial_timestamp).total_seconds() > manifest.duration_seconds for e in events):
        raise ValueError("event outside task duration")
    bindings = manifest.bindings()
    ids = [binding.id for binding in bindings]
    if len(ids) != len(set(ids)):
        raise ValueError("service binding IDs must be unique")
    adapters = {binding.id: registry.create(binding.service, binding.config) for binding in bindings}
    for binding in bindings:
        json.dumps(binding.config)
    if set(world) != set(adapters):
        raise ValueError("initial world state must match selected service bindings")
    world = {name: adapter.validate_state(world[name]) for name, adapter in adapters.items()}
    tool_names = []
    for binding in bindings:
        adapter = adapters[binding.id]
        for operation in manifest.tool_permissions.get(binding.id, []):
            if operation not in adapter.operations or not adapter.operations[operation].agent_visible:
                raise ValueError(f"invalid Agent tool permission: {binding.id}.{operation}")
            tool_names.append(binding.mcp_tool_name(operation))
    if len(tool_names) != len(set(tool_names)):
        raise ValueError("MCP tool names must be unique")
    if set(manifest.tool_permissions) - set(adapters):
        raise ValueError("tool permissions reference undeclared service")
    for event in events:
        if event.kind == "world_mutation":
            if event.adapter not in adapters:
                raise ValueError(f"event uses undeclared service: {event.adapter}")
            adapter = adapters[event.adapter]
            if event.operation not in adapter.operations or not adapter.is_mutating(event.operation):
                raise ValueError(f"event must use a mutating operation: {event.id}")
            adapter.validate_arguments(event.operation, event.arguments)
    package = TaskPackage(root=root, manifest=manifest, initial_world=world, events=events, context=contexts, rules=rules, judge_instructions=instructions, judge_evidence_sources=evidence_sources)
    package._registry = registry
    return package

