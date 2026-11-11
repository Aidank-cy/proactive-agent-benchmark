from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from .schema import NotificationRule


class OpportunityTracker:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.items: dict[str, dict[str, Any]] = {}

    def create(self, rule: NotificationRule, event_id: str, timestamp: str):
        oid = f"{rule.id}:{event_id}"
        if oid in self.items:
            return self.items[oid]
        deadline = datetime.fromisoformat(timestamp) + timedelta(seconds=rule.deadline_seconds)
        item = dict(opportunity_id=oid, task_id=self.task_id, trigger_event_id=event_id, created_at=timestamp, related_goal=rule.related_goal, notification_deadline=deadline.isoformat(), status="OPEN", notification_evidence=[], resolved_at=None, final_score=None, reason=None)
        self.items[oid] = item
        return item

    def settle(self, oid: str, status: str, score: float | None, timestamp: str, reason: str, refs: list[str]):
        item = self.items[oid]
        if item["status"] not in ("OPEN", "PENDING"):
            raise ValueError(f"opportunity already settled: {oid}")
        if status not in ("NOTIFIED", "RESOLVED", "EXPIRED", "CANCELLED"):
            raise ValueError(f"invalid settlement: {status}")
        if status in ("NOTIFIED", "EXPIRED") and score not in (-1, 0, 0.5, 1):
            raise ValueError("invalid required notification score")
        if status in ("RESOLVED", "CANCELLED") and score is not None:
            raise ValueError("closed obligations have no score")
        item.update(status=status, final_score=score, resolved_at=timestamp, reason=reason, notification_evidence=refs)

    def active(self):
        return [x for x in self.items.values() if x["status"] in ("OPEN", "PENDING")]

    def values(self):
        return list(self.items.values())


def aggregate(turns: list[dict[str, Any]], opportunities: list[dict[str, Any]]):
    task_scores = [t["judge_result"]["task"]["score"] for t in turns]
    required_scores = [o["final_score"] for o in opportunities if o["final_score"] is not None]
    quiet_scores = [t["judge_result"]["not_required_score"] for t in turns if t["judge_result"]["not_required_score"] is not None]
    req = mean(required_scores) if required_scores else None
    quiet = mean(quiet_scores) if quiet_scores else None
    notify = max(0, 0.5 * req + 0.5 * quiet) if req is not None and quiet is not None else None
    return {"R_task": max(0, sum(task_scores) / len(task_scores)) if task_scores else None, "R_notify": notify, "total_agent_turns": len(turns), "task_turn_scores": task_scores, "required_opportunity_count": len(required_scores), "R_required": req, "eligible_not_required_turn_count": len(quiet_scores), "R_not_required": quiet, "missed_notification_count": sum(o["status"] == "EXPIRED" for o in opportunities), "false_notification_count": sum(len(t["judge_result"]["notification"]["false_notifications"]) for t in turns), "outstanding_opportunities": [o for o in opportunities if o["status"] in ("OPEN", "PENDING")], "cancelled_opportunities": [o for o in opportunities if o["status"] == "CANCELLED"], "constraint_violations": [v for t in turns for v in t["judge_result"]["task"].get("constraint_violations", [])], "turn_judgments": [{"turn_id": t.get("turn_id"), "task": t["judge_result"]["task"], "notification": t["judge_result"]["notification"]} for t in turns], "opportunities": opportunities}

