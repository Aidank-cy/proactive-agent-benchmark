"""Bounded, authoritative evidence assembly for the independent Judge."""
from __future__ import annotations

import json
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
    def __init__(self, run_dir: Path, sources: EvidenceSources, history: Callable[[], list[dict[str, Any]]]):
        self.run_dir = run_dir.resolve()
        self.sources = sources
        self.history = history

    def _previous(self, evidence: dict[str, Any], previous: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        now = datetime.fromisoformat(evidence["simulated_timestamp"])
        current_id = evidence.get("turn_id")
        selected = previous[-limit:] if limit else []
        result = []
        for item in selected:
            turn_id = item["turn_id"]
            if not isinstance(turn_id, int) or turn_id < 1 or (current_id is not None and turn_id >= current_id):
                raise ValueError("Judge evidence references a future or invalid Turn")
            path = (self.run_dir / "evidence" / f"turn_{turn_id:04d}.json").resolve()
            if not path.is_relative_to(self.run_dir / "evidence"):
                raise ValueError("Judge evidence path escapes run directory")
            stored = json.loads(path.read_text(encoding="utf-8"))
            if stored.get("turn_id") != turn_id or stored.get("task_id") != evidence.get("task_id") or datetime.fromisoformat(stored["simulated_timestamp"]) > now:
                raise ValueError("Judge evidence contains future or unrelated data")
            prior_keys = {"task_id", "turn_id", "simulated_timestamp", "triggering_events", "goal_context_t", "world_pre", "world_post", "action_trace", "agent_response", "related_opportunities"}
            result.append({key: value for key, value in stored.items() if key in prior_keys})
        return result

    def assemble(self, evidence: dict[str, Any], previous: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> dict[str, Any]:
        selected = set(self.sources.sources)
        current = {key: evidence[key] for key in selected & evidence.keys() if key not in {"previous_turns", "backend_state", "world_history", "previous_notifications", "opportunities"}}
        current.update({key: evidence[key] for key in ("task_id", "turn_id", "tick_id", "simulated_timestamp") if key in evidence})
        turn_limit = self.sources.previous_turn_limit if "previous_turns" in selected else 0
        notification_limit = self.sources.previous_notification_limit if "previous_notifications" in selected else 0
        prior = self._previous(evidence, previous, max(turn_limit, notification_limit))
        payload: dict[str, Any] = {"evidence": current}
        if "previous_turns" in selected:
            payload["previous_evidence"] = prior[-turn_limit:] if turn_limit else []
        if "previous_notifications" in selected:
            payload["previous_notifications"] = [
                {"turn_id": row["turn_id"], "notifications": row.get("agent_response", {}).get("proactive_notifications", [])}
                for row in (prior[-notification_limit:] if notification_limit else []) if row.get("agent_response", {}).get("proactive_notifications")
            ]
        if "backend_state" in selected:
            payload["backend_state"] = evidence.get("world_post")
        if "world_history" in selected:
            now = datetime.fromisoformat(evidence["simulated_timestamp"])
            records = [row for row in self.history() if datetime.fromisoformat(row["at"]) <= now]
            payload["world_history"] = records[-self.sources.world_history_limit:] if self.sources.world_history_limit else []
        if "opportunities" in selected:
            payload["opportunities"] = [row for row in opportunities if datetime.fromisoformat(row["created_at"]) <= datetime.fromisoformat(evidence["simulated_timestamp"])]
        return payload
