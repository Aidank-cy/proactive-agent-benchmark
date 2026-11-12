"""Cross-Tick notification obligations and the two independent score aggregates."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from .schema import NotificationRule


class OpportunityTracker:
    """SQLite-backed opportunity lifecycle, independent of Agent memory."""

    def __init__(self, task_id: str, store=None):
        self.task_id = task_id
        self.store = store
        self.items: dict[str, dict[str, Any]] = {item["opportunity_id"]: item for item in store.opportunities()} if store else {}

    def proposal_id(self, rule: NotificationRule, event_id: str, dedupe_key: str | None = None) -> str:
        # Legacy proposals keep their old IDs. New proposals use a stable episode
        # key supplied by the Judge, so duplicate event IDs are not the identity.
        if dedupe_key is None:
            return f"{rule.id}:{event_id}"
        digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:16]
        return f"{rule.id}:{digest}"

    def create(self, rule: NotificationRule, event_id: str, timestamp: str, reason: str = "Judge created notification opportunity.", refs: list[str] | None = None, *, tick_id: int | None = None, turn_id: int | None = None, dedupe_key: str | None = None):
        episode = dedupe_key or event_id
        oid = self.proposal_id(rule, event_id, dedupe_key)
        if oid in self.items:
            existing = self.items[oid]
            if existing["rule_id"] != rule.id or existing["dedupe_key"] != episode:
                raise ValueError(f"opportunity identity collision: {oid}")
            return existing
        deadline = datetime.fromisoformat(timestamp) + timedelta(seconds=rule.deadline_seconds)
        item = dict(opportunity_id=oid, task_id=self.task_id, rule_id=rule.id, dedupe_key=episode,
                    trigger_event_id=event_id, origin_event_id=event_id, origin_tick_id=tick_id,
                    origin_turn_id=None, first_actionable_at=None, created_at=timestamp,
                    related_goal=rule.related_goal, notification_deadline=deadline.isoformat(),
                    status="OPEN", notification_evidence=refs or [], notification_attempts=[],
                    resolved_at=None, settled_tick_id=None, settled_turn_id=None,
                    final_score=None, reason=None, notification_id=None, notification_ids=[],
                    evaluation_status="valid")
        if self.store:
            self.store.create_opportunity(item, reason, refs or [], tick_id, turn_id)
        self.items[oid] = item
        return item

    def mark_actionable(self, oid: str, timestamp: str, turn_id: int, refs: list[str], reason: str, *, tick_id: int):
        item = self.items[oid]
        if item["status"] not in ("OPEN", "PENDING"):
            raise ValueError(f"opportunity already settled: {oid}")
        if item["origin_turn_id"] is not None:
            return item
        if datetime.fromisoformat(timestamp) > datetime.fromisoformat(item["notification_deadline"]):
            raise ValueError("first actionable Turn is after the notification deadline")
        updated = {**item, "origin_turn_id": turn_id, "first_actionable_at": timestamp}
        if self.store:
            self.store.update_opportunity(updated, item["status"], timestamp, reason, refs, tick_id=tick_id, turn_id=turn_id)
        self.items[oid] = updated
        return updated

    def _validate_notifications(self, oid: str, notification_ids: list[str], turn_id: int, *, allow_existing: bool = False):
        if len(notification_ids) != len(set(notification_ids)):
            raise ValueError("duplicate notification ID in assessment")
        item = self.items[oid]
        if self.store:
            for notification_id in notification_ids:
                sent_at = self.store.validate_delivery(notification_id, turn_id)
                if datetime.fromisoformat(sent_at) > datetime.fromisoformat(item["notification_deadline"]):
                    raise ValueError(f"notification missed deadline: {notification_id}")
                matched = self.store.conn.execute("SELECT 1 FROM opportunity_notifications WHERE opportunity_id=? AND notification_id=?", (oid, notification_id)).fetchone()
                if matched and not allow_existing:
                    raise ValueError(f"notification already matched to this opportunity: {notification_id}")

    def record_attempt(self, oid: str, quality: str, notification_ids: list[str], timestamp: str, reason: str, refs: list[str], *, tick_id: int, turn_id: int):
        if oid not in self.items:
            raise ValueError(f"unknown opportunity ID: {oid}")
        item = self.items[oid]
        if item["status"] not in ("OPEN", "PENDING"):
            raise ValueError(f"opportunity already settled: {oid}")
        if quality not in ("partial", "complete", "harmful") or not notification_ids:
            raise ValueError("invalid notification attempt")
        if item["origin_turn_id"] is None:
            raise ValueError("notification opportunity has no actionable Turn")
        self._validate_notifications(oid, notification_ids, turn_id)
        updated = {**item, "status": "PENDING", "notification_id": item.get("notification_id") or notification_ids[0],
                   "notification_ids": [*item["notification_ids"], *notification_ids]}
        attempt = {"tick_id": tick_id, "turn_id": turn_id, "at": timestamp,
                   "notification_ids": notification_ids, "quality": quality,
                   "reason": reason, "evidence_refs": refs}
        updated["notification_attempts"] = [*item["notification_attempts"], attempt]
        if self.store:
            self.store.record_attempt({**updated, "previous_status": item["status"]}, timestamp, quality, reason, refs, notification_ids, tick_id=tick_id, turn_id=turn_id)
        self.items[oid] = updated
        return updated

    def settle(self, oid: str, status: str, score: float | None, timestamp: str, reason: str, refs: list[str], notification_ids: list[str] | None = None, *, tick_id: int | None = None, turn_id: int | None = None, evaluation_status: str = "valid"):
        if oid not in self.items:
            raise ValueError(f"unknown opportunity ID: {oid}")
        item = self.items[oid]
        if item["status"] not in ("OPEN", "PENDING"):
            raise ValueError(f"opportunity already settled: {oid}")
        if status not in ("NOTIFIED", "RESOLVED", "EXPIRED", "CANCELLED"):
            raise ValueError(f"invalid settlement: {status}")
        if status in ("NOTIFIED", "EXPIRED") and score not in (-1, 0, 0.5, 1):
            raise ValueError("invalid required notification score")
        if status in ("RESOLVED", "CANCELLED") and score is not None:
            raise ValueError("closed obligations have no score")
        if status == "NOTIFIED" and not (notification_ids or item["notification_ids"]):
            raise ValueError("NOTIFIED settlement requires notification IDs")
        if status in ("RESOLVED", "CANCELLED") and notification_ids:
            raise ValueError("closed obligation cannot claim notifications")
        notification_ids = notification_ids or []
        if notification_ids:
            if turn_id is None and self.store:
                raise ValueError("notification settlement needs an Agent Turn")
            if self.store:
                self._validate_notifications(oid, notification_ids, turn_id)
        updated = {**item, "status": status, "final_score": score, "resolved_at": timestamp,
                   "settled_tick_id": tick_id, "settled_turn_id": turn_id, "reason": reason,
                   "notification_evidence": refs, "evaluation_status": evaluation_status,
                   "notification_id": item.get("notification_id") or (notification_ids[0] if notification_ids else None),
                   "notification_ids": [*item["notification_ids"], *notification_ids]}
        if self.store:
            self.store.update_opportunity(updated, item["status"], timestamp, reason, refs, notification_ids, tick_id, turn_id)
        self.items[oid] = updated
        return updated

    def expire(self, oid: str, timestamp: str, *, tick_id: int, turn_id: int | None = None, reason: str = "Notification deadline passed."):
        item = self.items[oid]
        if item["origin_turn_id"] is None:
            return self.settle(oid, "CANCELLED", None, timestamp, "Unassessable: no actionable Agent Turn before deadline.", ["tick:clock"], tick_id=tick_id, turn_id=turn_id, evaluation_status="unassessable")
        partial = any(attempt["quality"] == "partial" for attempt in item["notification_attempts"])
        return self.settle(oid, "EXPIRED", 0.5 if partial else 0, timestamp, reason, ["tick:clock"], tick_id=tick_id, turn_id=turn_id)

    def mark_pending(self, oid: str, timestamp: str, *, tick_id: int | None = None, turn_id: int | None = None):
        item = self.items[oid]
        if item["status"] == "OPEN":
            updated = {**item, "status": "PENDING"}
            if self.store:
                self.store.update_opportunity(updated, "OPEN", timestamp, "Awaiting notification or resolution.", [], tick_id=tick_id, turn_id=turn_id)
            self.items[oid] = updated

    def reload(self):
        if self.store:
            self.items = {item["opportunity_id"]: item for item in self.store.opportunities()}

    def active(self):
        return [item for item in self.items.values() if item["status"] in ("OPEN", "PENDING")]

    def values(self):
        return list(self.items.values())


def aggregate(turns: list[dict[str, Any]], opportunities: list[dict[str, Any]]):
    """Required mean weights opportunities; quiet mean weights eligible Turns."""
    task_scores = [turn["judge_result"]["task"]["score"] for turn in turns]
    required_scores = [item["final_score"] for item in opportunities if item["final_score"] is not None and item.get("evaluation_status") != "unassessable"]
    quiet_scores = [turn["judge_result"]["not_required_score"] for turn in turns if turn["judge_result"]["not_required_score"] is not None]
    req = mean(required_scores) if required_scores else None
    quiet = mean(quiet_scores) if quiet_scores else None
    notify = max(0, 0.5 * req + 0.5 * quiet) if req is not None and quiet is not None else None
    attribution = []
    for turn in turns:
        turn_id = turn.get("turn_id")
        owned = [item for item in opportunities if item.get("origin_turn_id") == turn_id]
        pending = any(item["status"] in ("OPEN", "PENDING") for item in owned)
        scored = [item["final_score"] for item in owned if item["final_score"] is not None]
        attribution.append({"turn_id": turn_id, "notification_evaluation": {
            "required_opportunities": [{key: item.get(key) for key in ("opportunity_id", "origin_tick_id", "origin_turn_id", "settled_tick_id", "settled_turn_id", "status", "final_score", "notification_ids")} for item in owned],
            "required_score": mean(scored) if scored and not pending else None,
            "not_required_score": turn["judge_result"].get("not_required_score"),
            "status": "PENDING" if pending else ("EXCLUDED_PENDING" if not owned and turn["judge_result"].get("not_required_score") is None else "FINALIZED"),
        }})
    return {
        "R_task": max(0, sum(task_scores) / len(task_scores)) if task_scores else None,
        "R_notify": notify, "total_agent_turns": len(turns), "task_turn_scores": task_scores,
        "required_opportunity_count": len(required_scores), "R_required": req,
        "eligible_not_required_turn_count": len(quiet_scores), "R_not_required": quiet,
        "missed_notification_count": sum(item["status"] == "EXPIRED" and item["final_score"] == 0 for item in opportunities),
        "false_notification_count": sum(len(turn["judge_result"]["notification"]["false_notifications"]) for turn in turns),
        "outstanding_opportunities": [item for item in opportunities if item["status"] in ("OPEN", "PENDING")],
        "cancelled_opportunities": [item for item in opportunities if item["status"] == "CANCELLED"],
        "unassessable_opportunities": [item for item in opportunities if item.get("evaluation_status") == "unassessable"],
        "constraint_violations": [v for turn in turns for v in turn["judge_result"]["task"].get("constraint_violations", [])],
        "turn_judgments": [{"turn_id": turn.get("turn_id"), "task": turn["judge_result"]["task"], "notification": turn["judge_result"]["notification"]} for turn in turns],
        "per_turn_notification_attribution": attribution,
        "opportunities": opportunities,
    }
