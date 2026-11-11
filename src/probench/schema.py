from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps require a timezone offset")
    return value


class Manifest(BaseModel):
    task_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    initial_timestamp: datetime
    duration_seconds: int = Field(ge=0)
    adapters: list[str] = Field(default_factory=lambda: ["kv"])
    workspace_files: dict[str, str] = Field(default_factory=dict)
    tool_permissions: dict[str, list[str]] = Field(default_factory=dict)
    judge: dict[str, Any] = Field(default_factory=dict)
    initial_world: str = "initial_world.yaml"
    events: str = "events.yaml"
    context: str = "context.yaml"
    notification_rules: str = "judge/notification_rules.yaml"

    _timestamp = field_validator("initial_timestamp")(aware)


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
    root: Path
    manifest: Manifest
    initial_world: dict[str, Any]
    events: list[Event]
    context: list[ContextDisclosure]
    rules: list[NotificationRule]

    def goal_context(self, disclosed_event_ids: set[str]) -> dict[str, Any]:
        rows = [c.model_dump() for c in self.context if c.event_id in disclosed_event_ids]
        return {"disclosures": rows}


def _read(root: Path, relative: str) -> Any:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes task root: {relative}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_task(path: str | Path) -> TaskPackage:
    root = Path(path).resolve()
    manifest = Manifest.model_validate(_read(root, "manifest.yaml"))
    world = _read(root, manifest.initial_world) or {}
    events = [Event.model_validate(x) for x in (_read(root, manifest.events) or [])]
    contexts = [ContextDisclosure.model_validate(x) for x in (_read(root, manifest.context) or [])]
    rules = [NotificationRule.model_validate(x) for x in (_read(root, manifest.notification_rules) or [])]
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
    if any(e.adapter not in manifest.adapters for e in events if e.kind == "world_mutation"):
        raise ValueError("event uses undeclared adapter")
    return TaskPackage(root=root, manifest=manifest, initial_world=world, events=events, context=contexts, rules=rules)

