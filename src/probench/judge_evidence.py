"""Bounded, authoritative evidence assembly for the independent Judge."""
from __future__ import annotations

import json
import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, model_validator


Source = Literal[
    "goal_context_t", "world_pre", "world_post", "triggering_events",
    "action_trace", "tool_results", "agent_response", "previous_turns",
    "backend_state", "world_history", "previous_notifications", "opportunities",
]


class EvidenceSources(BaseModel):
    """任务可选的证据源及历史上限；当前 Turn 的关键证据不可省略。"""

    sources: list[Source] = Field(default_factory=lambda: [
        "goal_context_t", "world_pre", "world_post", "triggering_events",
        "action_trace", "tool_results", "agent_response", "previous_turns",
        "world_history", "previous_notifications", "opportunities",
    ])
    previous_turn_limit: int = Field(default=3, ge=0, le=100)
    previous_notification_limit: int = Field(default=50, ge=0, le=500)
    world_history_limit: int = Field(default=30, ge=0, le=1000)

    @model_validator(mode="after")
    def require_current_evidence(self):
        required = {"goal_context_t", "world_pre", "world_post", "triggering_events", "action_trace", "tool_results", "agent_response", "opportunities"}
        if not required.issubset(self.sources):
            raise ValueError(f"Judge evidence sources must include {sorted(required)}")
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("Judge evidence sources must be unique")
        return self


class JudgeEvidenceLoader:
    """从本次运行中装配 Judge 实际可读的证据，而非只传文件路径。"""

    def __init__(self, run_dir: Path, sources: EvidenceSources, history: Callable[[], list[dict[str, Any]]], store=None):
        self.run_dir = run_dir.resolve()
        self.sources = sources
        self.history = history
        self.store = store
        self._cache_key = None
        self._cache_value = None

    def _previous(self, evidence: dict[str, Any], previous: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        """读取最多 `limit` 个已冻结的过去 Turn，并排除跨任务或未来记录。"""
        now = datetime.fromisoformat(evidence["simulated_timestamp"])
        current_id = evidence.get("turn_id")
        selected = previous[-limit:] if limit else []
        result = []
        for item in selected:
            turn_id = item["turn_id"]
            if not isinstance(turn_id, int) or turn_id < 1 or (current_id is not None and turn_id >= current_id):
                raise ValueError("Judge evidence references a future or invalid Turn")
            path = (self.run_dir / "evidence" / f"turn_{turn_id:04d}.json").resolve()
            # resolve 后检查边界，也能挡住指向运行目录外的符号链接。
            if not path.is_relative_to(self.run_dir / "evidence"):
                raise ValueError("Judge evidence path escapes run directory")
            stored = json.loads(path.read_text(encoding="utf-8"))
            if stored.get("turn_id") != turn_id or stored.get("task_id") != evidence.get("task_id") or datetime.fromisoformat(stored["simulated_timestamp"]) > now:
                raise ValueError("Judge evidence contains future or unrelated data")
            # 只给 Judge 与判断历史进展有关的字段，避免发送整个工作区快照。
            prior_keys = {"task_id", "turn_id", "simulated_timestamp", "triggering_events", "goal_context_t", "world_pre", "world_post", "action_trace", "agent_response", "related_opportunities"}
            result.append({key: value for key, value in stored.items() if key in prior_keys})
        return result

    def assemble(self, evidence: dict[str, Any], previous: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> dict[str, Any]:
        """组装当前 Tick 的统一 Judge 输入；历史记录只取已披露且有界的部分。"""
        cache_key = (evidence.get("tick_id"), evidence.get("turn_id"), tuple(item["turn_id"] for item in previous),
                     tuple((item["opportunity_id"], item["status"], item.get("final_score"), len(item.get("notification_attempts", []))) for item in opportunities))
        if cache_key == self._cache_key:
            return copy.deepcopy(self._cache_value)
        selected = set(self.sources.sources)
        turn_limit = self.sources.previous_turn_limit if "previous_turns" in selected else 0
        notification_limit = self.sources.previous_notification_limit if "previous_notifications" in selected else 0
        prior = self._previous(evidence, previous, max(turn_limit, notification_limit))
        now = datetime.fromisoformat(evidence["simulated_timestamp"])
        event_ids = [event["id"] for event in evidence.get("triggering_events", [])]
        if self.store:
            current_changes = self.store.changes_for(event_ids, evidence.get("turn_id"), evidence["simulated_timestamp"])
            history = self.store.history_until(evidence["simulated_timestamp"], self.sources.world_history_limit) if "world_history" in selected else []
        else:
            history = [row for row in self.history() if datetime.fromisoformat(row["at"]) <= now]
            current_changes = [
                {key: row.get(key) for key in ("seq", "tick_id", "event_id", "turn_id", "action_id", "adapter", "operation", "changes", "patch")}
                for row in history if row.get("event_id") in event_ids or (evidence.get("turn_id") is not None and row.get("turn_id") == evidence["turn_id"])
            ]
        relevant: dict[str, Any] = {}
        if "previous_turns" in selected:
            relevant["previous_turns"] = prior[-turn_limit:] if turn_limit else []
        if "previous_notifications" in selected:
            relevant["previous_notifications"] = [
                {"turn_id": row["turn_id"], "notifications": row.get("agent_response", {}).get("proactive_notifications", [])}
                for row in (prior[-notification_limit:] if notification_limit else []) if row.get("agent_response", {}).get("proactive_notifications")
            ]
        if "backend_state" in selected:
            relevant["backend_state"] = evidence.get("world_post")
        if "world_history" in selected:
            # 即使提供了更长的后端历史，也只发送当前模拟时刻之前的末尾记录。
            relevant["world_history"] = history[-self.sources.world_history_limit:] if self.sources.world_history_limit else []
        visible_opportunities = []
        for opportunity in opportunities:
            if datetime.fromisoformat(opportunity["created_at"]) > now:
                continue
            row = copy.deepcopy(opportunity)
            row["notification_attempts"] = [attempt for attempt in row.get("notification_attempts", []) if datetime.fromisoformat(attempt["at"]) <= now]
            if any(datetime.fromisoformat(notice["timestamp"]) > now for attempt in row["notification_attempts"] for notice in attempt.get("notifications", [])):
                raise ValueError("Judge opportunity contains a future notification")
            if row.get("first_actionable_at") and datetime.fromisoformat(row["first_actionable_at"]) > now:
                row["origin_turn_id"] = None
                row["first_actionable_at"] = None
            if row.get("resolved_at") and datetime.fromisoformat(row["resolved_at"]) > now:
                row.update(status="PENDING" if row.get("origin_turn_id") is not None else "OPEN",
                           resolved_at=None, final_score=None, reason=None,
                           settled_tick_id=None, settled_turn_id=None,
                           notification_evidence=[], evaluation_status="valid")
                row["notification_ids"] = [notification_id for attempt in row["notification_attempts"] for notification_id in attempt["notification_ids"]]
                row["notification_id"] = row["notification_ids"][0] if row["notification_ids"] else None
            visible_opportunities.append(row)
        relevant["opportunities"] = visible_opportunities
        events = self.store.events_for_ids(event_ids) if self.store else evidence.get("triggering_events", [])
        if any(datetime.fromisoformat(event["at"]) > now for event in events):
            raise ValueError("Judge evidence includes a future event")
        bundle = {
            "tick": {"task_id": evidence["task_id"], "tick_id": evidence["tick_id"], "turn_id": evidence.get("turn_id"),
                     "simulated_timestamp": evidence["simulated_timestamp"], "event_ids": event_ids},
            "events": events,
            "goal_context": evidence["goal_context_t"],
            "world_state": {"pre": evidence["world_pre"], "post": evidence["world_post"]},
            "world_changes": current_changes,
            "actions": evidence.get("action_trace", []),
            "agent_response": evidence.get("agent_response", {}),
            "current_notifications": evidence.get("agent_response", {}).get("proactive_notifications", []),
            "active_opportunities": [row for row in visible_opportunities if row["status"] in ("OPEN", "PENDING")],
            "relevant_history": relevant,
        }
        self._cache_key, self._cache_value = cache_key, copy.deepcopy(bundle)
        return bundle
