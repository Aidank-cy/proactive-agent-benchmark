import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from probench.agent import MockAgent
from probench.judge import MockJudge, NotificationVerdict, Settlement
from probench.run_store import RunStore
from probench.runtime import run
from probench.schema import NotificationRule, load_task
from probench.scoring import OpportunityTracker
from probench.world import KVAdapter, SQLiteWorld


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    task = load_task(ROOT / "examples/synthetic")
    script = json.loads((task.root / "mock_agent.json").read_text())
    return task, script


def test_text_only_notification_settles_without_hidden_id(tmp_path):
    task, script = fixture()
    result = run(task, MockAgent(script), MockJudge(), tmp_path / "run")
    db = sqlite3.connect(tmp_path / "run/world.db")
    notification_id, action_id, legacy_id = db.execute("SELECT notification_id,action_id,opportunity_id FROM notifications").fetchone()
    action_args = json.loads(db.execute("SELECT arguments FROM mcp_actions WHERE action_id=?", (action_id,)).fetchone()[0])
    assert action_args == {"text": "The signal changed."}
    assert legacy_id is None
    assert result["opportunities"][0]["notification_ids"] == [notification_id]
    assert result["turn_judgments"][2]["notification"]["settled"][0]["notification_ids"] == [notification_id]
    assert db.execute("SELECT notification_ids FROM opportunity_history WHERE to_status='NOTIFIED'").fetchone()[0] == json.dumps([notification_id], ensure_ascii=False)
    db.close()


def test_multiple_opportunities_and_notifications_match_by_judge_ids(tmp_path):
    task, script = fixture()
    task.rules.append(NotificationRule(id="material_item", description="Synthetic second condition", trigger_event_ids=["change"], deadline_seconds=150, related_goal="Maintain item."))
    script[2]["proactive_notifications"].append({"text": "The item is at 1."})

    class TwoOpportunityJudge(MockJudge):
        def notification(self, evidence, rules, opportunities, previous):
            base = super().notification(evidence, rules, opportunities, previous)
            notices = {row["text"]: row["notification_id"] for row in evidence["agent_response"]["proactive_notifications"]}
            matches = {"material_signal:change": "The signal changed.", "material_item:change": "The item is at 1."}
            settled = [Settlement(opportunity_id=row["opportunity_id"], status="NOTIFIED", score=1, notification_ids=[notices[matches[row["opportunity_id"]]]], reason="Content and world match.", evidence_refs=["turn:notification", "world:post"])
                       for row in opportunities if row["status"] in ("OPEN", "PENDING") and matches.get(row["opportunity_id"]) in notices]
            used = {nid for item in settled for nid in item.notification_ids}
            return NotificationVerdict(new_opportunities=base.new_opportunities, settled=settled, false_notifications=[nid for nid in notices.values() if nid not in used], explanation="Matched by content and world state.", evidence_refs=["world:post"])

    path = tmp_path / "run"
    assert run(task, MockAgent(script), TwoOpportunityJudge(), path, max_ticks=2)["status"] == "paused"
    result = run(task, MockAgent(script), TwoOpportunityJudge(), path, resume=True)
    assert result["required_opportunity_count"] == 2
    assert result["R_required"] == 1
    assert len({row["notification_ids"][0] for row in result["opportunities"]}) == 2
    db = sqlite3.connect(path / "world.db")
    rows = db.execute("SELECT opportunity_id,notification_id FROM opportunity_notifications ORDER BY opportunity_id").fetchall()
    assert len(rows) == 2 and rows[0][1] != rows[1][1]
    assert db.execute("SELECT COUNT(*) FROM opportunity_history WHERE to_status='NOTIFIED'").fetchone()[0] == 2
    db.close()


def test_one_opportunity_keeps_all_matched_notifications(tmp_path):
    task, script = fixture()
    script[2]["proactive_notifications"] = [{"text": "The signal"}, {"text": "changed."}]

    class CombinedJudge(MockJudge):
        def notification(self, evidence, rules, opportunities, previous):
            base = super().notification(evidence, rules, opportunities, previous)
            notices = evidence["agent_response"]["proactive_notifications"]
            settled = [Settlement(opportunity_id=opportunities[0]["opportunity_id"], status="NOTIFIED", score=1, notification_ids=[row["notification_id"] for row in notices], reason="Both messages jointly describe the change.", evidence_refs=["turn:notification", "world:post"])] if notices and opportunities else []
            return NotificationVerdict(new_opportunities=base.new_opportunities, settled=settled, explanation="Combined content checked.", evidence_refs=["world:post"])

    result = run(task, MockAgent(script), CombinedJudge(), tmp_path / "run")
    ids = result["opportunities"][0]["notification_ids"]
    assert len(ids) == 2
    db = sqlite3.connect(tmp_path / "run/world.db")
    assert db.execute("SELECT COUNT(*) FROM opportunity_notifications").fetchone()[0] == 2
    assert json.loads(db.execute("SELECT notification_ids FROM opportunity_history WHERE to_status='NOTIFIED'").fetchone()[0]) == ids
    db.close()


def test_unrelated_and_duplicate_notifications_are_false_ids(tmp_path):
    task, script = fixture()
    script[2]["proactive_notifications"] = [{"text": "The signal changed."}, {"text": "The signal changed."}, {"text": "Unrelated update."}]
    result = run(task, MockAgent(script), MockJudge(), tmp_path / "run")
    ids = [row["notification_id"] for row in json.loads((tmp_path / "run/turns/turn_0003.json").read_text())["agent_response"]["proactive_notifications"]]
    assert result["opportunities"][0]["notification_ids"] == ids[:1]
    assert result["turn_judgments"][2]["notification"]["false_notifications"] == ids[1:]
    assert result["false_notification_count"] == 2


def test_schema_and_runtime_reject_invalid_matching(tmp_path):
    with pytest.raises(ValidationError, match="requires notification_ids"):
        Settlement(opportunity_id="x", status="NOTIFIED", score=1, reason="x", evidence_refs=["turn:notification"])
    with pytest.raises(ValidationError, match="duplicate notification ID"):
        Settlement(opportunity_id="x", status="NOTIFIED", score=1, notification_ids=["n", "n"], reason="x", evidence_refs=["turn:notification"])
    task, script = fixture()

    class InvalidJudge(MockJudge):
        def notification(self, evidence, rules, opportunities, previous):
            base = super().notification(evidence, rules, opportunities, previous)
            if evidence["agent_response"]["proactive_notifications"]:
                base.settled[0].notification_ids = ["nonexistent"]
            return base

    with pytest.raises(ValueError, match="outside the current Turn"):
        run(task, MockAgent(script), InvalidJudge(), tmp_path / "invalid")

    second_task, second_script = fixture()
    second_task.rules.append(NotificationRule(id="second_rule", description="Synthetic second condition", trigger_event_ids=["change"], deadline_seconds=150, related_goal="Maintain item."))

    class DoubleMatchJudge(MockJudge):
        def notification(self, evidence, rules, opportunities, previous):
            base = super().notification(evidence, rules, opportunities, previous)
            notices = evidence["agent_response"]["proactive_notifications"]
            if notices and len(opportunities) == 2:
                notification_id = notices[0]["notification_id"]
                base.settled = [Settlement(opportunity_id=row["opportunity_id"], status="NOTIFIED", score=1, notification_ids=[notification_id], reason="Claimed twice.", evidence_refs=["turn:notification"]) for row in opportunities]
            return base

    with pytest.raises(ValueError, match="reused one notification"):
        run(second_task, MockAgent(second_script), DoubleMatchJudge(), tmp_path / "double")


def test_tracker_rejects_duplicate_use_wrong_turn_and_failed_action(tmp_path):
    world = SQLiteWorld(tmp_path / "world.db", {"kv": {}}, {"kv": KVAdapter()})
    store = RunStore(world)
    tracker = OpportunityTracker("synthetic", store)
    rule = NotificationRule(id="r", description="x", deadline_seconds=180, related_goal="x")
    a = tracker.create(rule, "event_a", "2026-01-01T00:00:00+00:00")
    b = tracker.create(rule, "event_b", "2026-01-01T00:00:00+00:00")
    at = "2026-01-01T00:01:00+00:00"
    nid = world.notify(1, at, "message", action_id="action:1")
    world.record_mcp_action(1, at, "notify_user", {"text": "message"}, {"notification_id": nid}, True, None, {}, action_id="action:1", notification_id=nid)
    with pytest.raises(ValueError, match="current Turn"):
        tracker.settle(a["opportunity_id"], "NOTIFIED", 1, at, "x", [], [nid], tick_id=2, turn_id=2)
    tracker.settle(a["opportunity_id"], "NOTIFIED", 1, at, "x", [], [nid], tick_id=1, turn_id=1)
    with pytest.raises(ValueError, match="already matched"):
        tracker.settle(b["opportunity_id"], "NOTIFIED", 1, at, "x", [], [nid], tick_id=1, turn_id=1)
    assert tracker.items[b["opportunity_id"]]["status"] == "OPEN"
    with pytest.raises(ValueError, match="already settled"):
        tracker.settle(a["opportunity_id"], "NOTIFIED", 1, at, "x", [], [nid], tick_id=1, turn_id=1)
    bad = world.notify(1, at, "failed", action_id="action:2")
    world.record_mcp_action(1, at, "notify_user", {"text": "failed"}, None, False, "failed", {}, action_id="action:2", notification_id=bad)
    with pytest.raises(ValueError, match="successful notify_user"):
        tracker.settle(b["opportunity_id"], "NOTIFIED", 1, at, "x", [], [bad], tick_id=1, turn_id=1)
    world.close()


def test_deadline_uses_actual_notification_timestamp_and_scores_unchanged(tmp_path):
    task, script = fixture()
    task.rules[0].deadline_seconds = 30
    result = run(task, MockAgent(script), MockJudge(), tmp_path / "late")
    late_id = result["opportunities"][0]["notification_ids"][0]
    assert result["opportunities"][0]["status"] == "EXPIRED"
    assert result["opportunities"][0]["final_score"] == 0
    assert result["turn_judgments"][2]["notification"]["false_notifications"] == [late_id]
    assert result["R_task"] == 0.5
    assert result["R_notify"] == max(0, 0.5 * result["R_required"] + 0.5 * result["R_not_required"])
