"""跨 Turn 通知机会跟踪和两个独立指标的汇总。"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from .schema import NotificationRule


class OpportunityTracker:
    """独立于 Agent 记忆保存跨 Turn 通知义务及其最终结算。"""

    def __init__(self, task_id: str, store=None):
        self.task_id = task_id
        self.store = store
        self.items: dict[str, dict[str, Any]] = {item["opportunity_id"]: item for item in store.opportunities()} if store else {}

    def create(self, rule: NotificationRule, event_id: str, timestamp: str, reason: str = "Judge created notification opportunity.", refs: list[str] | None = None, *, tick_id: int | None = None, turn_id: int | None = None):
        # 同一规则与事件只对应一次机会，避免重复计分。
        oid = f"{rule.id}:{event_id}"
        if oid in self.items:
            return self.items[oid]
        deadline = datetime.fromisoformat(timestamp) + timedelta(seconds=rule.deadline_seconds)
        item = dict(opportunity_id=oid, task_id=self.task_id, trigger_event_id=event_id, created_at=timestamp, related_goal=rule.related_goal, notification_deadline=deadline.isoformat(), status="OPEN", notification_evidence=[], resolved_at=None, final_score=None, reason=None, notification_id=None)
        self.items[oid] = item
        if self.store:
            self.store.create_opportunity(item, reason, refs or [], tick_id, turn_id)
        return item

    def settle(self, oid: str, status: str, score: float | None, timestamp: str, reason: str, refs: list[str], notification_id: str | None = None, *, tick_id: int | None = None, turn_id: int | None = None):
        """最终关闭一项义务；已关闭的机会不能再次获得通知分数。

        NOTIFIED/EXPIRED 有必需通知分数，RESOLVED/CANCELLED 则仅记录
        关闭原因，不充当必需通知的统计样本。
        """
        item = self.items[oid]
        old_status = item["status"]
        if item["status"] not in ("OPEN", "PENDING"):
            raise ValueError(f"opportunity already settled: {oid}")
        if status not in ("NOTIFIED", "RESOLVED", "EXPIRED", "CANCELLED"):
            raise ValueError(f"invalid settlement: {status}")
        if status in ("NOTIFIED", "EXPIRED") and score not in (-1, 0, 0.5, 1):
            raise ValueError("invalid required notification score")
        if status in ("RESOLVED", "CANCELLED") and score is not None:
            raise ValueError("closed obligations have no score")
        if notification_id and self.store and not self.store.conn.execute("SELECT 1 FROM notifications WHERE notification_id=?", (notification_id,)).fetchone():
            raise ValueError(f"unknown notification ID: {notification_id}")
        item.update(status=status, final_score=score, resolved_at=timestamp, reason=reason, notification_evidence=refs, notification_id=notification_id)
        if self.store:
            self.store.update_opportunity(item, old_status, timestamp, reason, refs, notification_id, tick_id, turn_id)

    def mark_pending(self, oid: str, timestamp: str, *, tick_id: int | None = None, turn_id: int | None = None):
        item = self.items[oid]
        if item["status"] == "OPEN":
            item["status"] = "PENDING"
            if self.store:
                self.store.update_opportunity(item, "OPEN", timestamp, "Awaiting notification or resolution.", [], tick_id=tick_id, turn_id=turn_id)

    def reload(self):
        """在新 Tick 开始时从 SQLite 恢复未结算和已结算机会。"""
        if self.store:
            self.items = {item["opportunity_id"]: item for item in self.store.opportunities()}

    def active(self):
        return [x for x in self.items.values() if x["status"] in ("OPEN", "PENDING")]

    def values(self):
        return list(self.items.values())


def aggregate(turns: list[dict[str, Any]], opportunities: list[dict[str, Any]]):
    """分别汇总 R_task 和 R_notify，绝不合成一个总分。

    R_task 对所有 Agent Turn 求均值后取零下界。R_notify 先对“必需通知
    机会”和“无需通知的合格 Turn”各自求均值，再按固定 0.5 权重合成；
    任一类别无样本时保留 N/A，不擅自给该类别满分或改权重。
    """
    task_scores = [t["judge_result"]["task"]["score"] for t in turns]
    required_scores = [o["final_score"] for o in opportunities if o["final_score"] is not None]
    quiet_scores = [t["judge_result"]["not_required_score"] for t in turns if t["judge_result"]["not_required_score"] is not None]
    req = mean(required_scores) if required_scores else None
    quiet = mean(quiet_scores) if quiet_scores else None
    notify = max(0, 0.5 * req + 0.5 * quiet) if req is not None and quiet is not None else None
    return {"R_task": max(0, sum(task_scores) / len(task_scores)) if task_scores else None, "R_notify": notify, "total_agent_turns": len(turns), "task_turn_scores": task_scores, "required_opportunity_count": len(required_scores), "R_required": req, "eligible_not_required_turn_count": len(quiet_scores), "R_not_required": quiet, "missed_notification_count": sum(o["status"] == "EXPIRED" for o in opportunities), "false_notification_count": sum(len(t["judge_result"]["notification"]["false_notifications"]) for t in turns), "outstanding_opportunities": [o for o in opportunities if o["status"] in ("OPEN", "PENDING")], "cancelled_opportunities": [o for o in opportunities if o["status"] == "CANCELLED"], "constraint_violations": [v for t in turns for v in t["judge_result"]["task"].get("constraint_violations", [])], "turn_judgments": [{"turn_id": t.get("turn_id"), "task": t["judge_result"]["task"], "notification": t["judge_result"]["notification"]} for t in turns], "opportunities": opportunities}

