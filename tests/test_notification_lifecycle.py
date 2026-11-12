"""Cross-Tick notification lifecycle contracts using a deterministic Judge double."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from probench.agent import MockAgent
from probench.judge import (ActionabilityAssessment, MockJudge, NotificationAssessment,
                            NotificationVerdict, OpportunityProposal, Settlement)
from probench.judge_evidence import JudgeEvidenceLoader
from probench.run_store import RunStore
from probench.runtime import run, verify_evidence
from probench.schema import Event, NotificationRule, load_task
from probench.scoring import aggregate
from probench.world import SQLiteWorld


FIXTURE = Path(__file__).resolve().parent / "fixtures/synthetic"
BASE = "2026-01-01T00:{minute:02d}:{second:02d}+00:00"


def event(id, minute, second, kind="scheduled_wakeup", **extra):
    return Event(id=id, at=BASE.format(minute=minute, second=second), kind=kind, **extra)


def scenario(*, silent=True, deadline=120, notices=None, extra_events=None, second_rule=False):
    task = load_task(FIXTURE)
    change = event("change", 1, 0, "world_mutation", operation="set", arguments={"key": "signal", "value": "changed"})
    task.events = [event("user_start", 0, 0, "user_message", body="Track my item."), change]
    if not silent:
        task.events.append(event("wake_change", 1, 0))
    task.events.extend([event("wake_a", 1, 10), event("wake_b", 2, 0), event("wake_c", 3, 0), event("wake_d", 4, 0)])
    if extra_events:
        task.events.extend(extra_events)
        task.events.sort(key=lambda row: row.at)
    task.rules = [NotificationRule(id="signal", description="Potentially material signal.", trigger_event_ids=["change"], deadline_seconds=deadline, related_goal="Track item.")]
    if second_rule:
        task.rules.append(NotificationRule(id="item", description="Separate item issue.", trigger_event_ids=["change"], deadline_seconds=deadline, related_goal="Track item."))
    notices = notices or {}
    wake_count = len({e.at for e in task.events if e.kind in ("user_message", "notification", "scheduled_wakeup")})
    script = [{"response": "Observed.", "proactive_notifications": [{"text": text} for text in notices.get(i, [])]} for i in range(1, wake_count + 1)]
    return task, script


class ScenarioJudge(MockJudge):
    def __init__(self, decide):
        self.decide = decide

    def notification(self, evidence, rules, opportunities, previous):
        return self.decide(evidence, rules, opportunities)


def verdict(*, proposals=(), actionable=(), assessments=(), settled=(), false=()):
    return NotificationVerdict(new_opportunities=list(proposals), actionable=list(actionable),
                               assessments=list(assessments), settled=list(settled),
                               false_notifications=list(false), explanation="Deterministic lifecycle scenario.", evidence_refs=["world:post"])


def proposal(rule="signal", event_id="change", key="signal:order-1:cancel-cycle-1", actionable=False):
    return OpportunityProposal(rule_id=rule, trigger_event_id=event_id, dedupe_key=key,
                               actionable_now=actionable, explanation="Current state affects the goal.",
                               evidence_refs=[f"event:{event_id}", "world:post", "goal:context"])


def actionability(oid):
    return ActionabilityAssessment(opportunity_id=oid, explanation="This Turn can inspect the authorized world tool.", evidence_refs=["world:post"])


def assessment(oid, ids, quality):
    return NotificationAssessment(opportunity_id=oid, notification_ids=ids, quality=quality,
                                  explanation=f"Judge assessed {quality} content against the current world.",
                                  evidence_refs=["turn:notification", "world:post"])


def notices_in(evidence):
    return [row["notification_id"] for row in evidence["agent_response"]["proactive_notifications"]]


def basic_decider(evidence, rules, opportunities, *, quality_by_turn=None, false_by_turn=None, resolve_turn=None, cancel_turn=None, duplicate=False):
    quality_by_turn = quality_by_turn or {}
    false_by_turn = false_by_turn or set()
    turn = evidence.get("turn_id")
    ids = {row["id"] for row in evidence["triggering_events"]}
    proposals = [proposal()] if "change" in ids else []
    if duplicate and "duplicate" in ids:
        proposals.append(proposal(event_id="duplicate"))
    open_rows = [row for row in opportunities if row["status"] in ("OPEN", "PENDING")]
    actionable = [actionability(row["opportunity_id"]) for row in open_rows if turn and row["origin_turn_id"] is None]
    oid = open_rows[0]["opportunity_id"] if open_rows else None
    if oid and turn == resolve_turn:
        return verdict(proposals=proposals, actionable=actionable, settled=[Settlement(opportunity_id=oid, status="RESOLVED", reason="Agent independently fixed the issue.", evidence_refs=["world:post"])])
    if oid and turn == cancel_turn:
        return verdict(proposals=proposals, actionable=actionable, settled=[Settlement(opportunity_id=oid, status="CANCELLED", reason="User cancelled the goal.", evidence_refs=["goal:context"])])
    current = notices_in(evidence)
    if current and turn in false_by_turn:
        return verdict(proposals=proposals, actionable=actionable, false=current)
    assessed = [assessment(oid, current, quality_by_turn[turn])] if current and oid and turn in quality_by_turn else []
    return verdict(proposals=proposals, actionable=actionable, assessments=assessed,
                   false=current if current and not assessed else [])


def test_silent_origin_cross_turn_completion_and_attribution(tmp_path):
    task, script = scenario(notices={4: ["accurate update"]})
    judge = ScenarioJudge(lambda e, r, o: basic_decider(e, r, o, quality_by_turn={4: "complete"}))
    result = run(task, MockAgent(script), judge, tmp_path / "run")
    item = result["opportunities"][0]
    assert (item["origin_tick_id"], item["origin_turn_id"], item["settled_turn_id"]) == (2, 2, 4)
    assert item["first_actionable_at"] == BASE.format(minute=1, second=10)
    assert item["status"] == "NOTIFIED" and item["final_score"] == 1
    assert result["per_turn_notification_attribution"][1]["notification_evaluation"]["required_score"] == 1
    assert result["per_turn_notification_attribution"][3]["notification_evaluation"]["required_opportunities"] == []
    assert result["R_required"] == 1 and verify_evidence(tmp_path / "run")
    with sqlite3.connect(tmp_path / "run/world.db") as db:
        applied = json.loads(db.execute("SELECT applied_result FROM judgments WHERE tick_id=? AND kind='notification'", (item["settled_tick_id"],)).fetchone()[0])
        assert applied["opportunity_history_ids"] and applied["false_notification_ids"] == []
    frozen = json.loads((tmp_path / "run/evidence/turn_0002.json").read_text())
    assert "judge_result" not in frozen


def test_same_turn_creation_and_notification(tmp_path):
    task, script = scenario(silent=False, notices={2: ["accurate"]})
    def decide(e, rules, opportunities):
        if e["tick_id"] == 2:
            p = proposal(actionable=True)
            return verdict(proposals=[p], assessments=[NotificationAssessment(
                rule_id=p.rule_id, dedupe_key=p.dedupe_key, notification_ids=notices_in(e),
                quality="complete", explanation="The current message fully covers the new problem.",
                evidence_refs=["turn:notification", "world:post"])])
        return verdict()
    result = run(task, MockAgent(script), ScenarioJudge(decide), tmp_path / "run")
    item = result["opportunities"][0]
    assert item["origin_turn_id"] == item["settled_turn_id"] == 2
    assert item["final_score"] == 1


@pytest.mark.parametrize("quality,expected", [(None, 0), ("partial", 0.5), ("harmful", -1)])
def test_deadline_and_partial_or_harmful_result(tmp_path, quality, expected):
    task, script = scenario(deadline=30, notices={2: ["message"]} if quality else {})
    judge = ScenarioJudge(lambda e, r, o: basic_decider(e, r, o, quality_by_turn={2: quality} if quality else {}))
    result = run(task, MockAgent(script), judge, tmp_path / "run")
    item = result["opportunities"][0]
    assert item["final_score"] == expected
    assert item["status"] == ("NOTIFIED" if quality == "harmful" else "EXPIRED")
    assert result["R_required"] == expected
    assert len(item["notification_attempts"]) == (1 if quality else 0)


def test_partial_then_complete_across_turns_and_resume(tmp_path):
    task, script = scenario(deadline=150, notices={2: ["initial detail"], 4: ["corrected complete detail"]})
    judge = ScenarioJudge(lambda e, r, o: basic_decider(e, r, o, quality_by_turn={2: "partial", 4: "complete"}))
    path = tmp_path / "run"
    assert run(task, MockAgent(script), judge, path, max_ticks=3)["status"] == "paused"
    resumed = run(task, MockAgent(script), judge, path, resume=True)
    item = resumed["opportunities"][0]
    assert item["final_score"] == 1 and item["settled_turn_id"] == 4
    assert [attempt["quality"] for attempt in item["notification_attempts"]] == ["partial", "complete"]
    assert len(item["notification_ids"]) == 2
    with sqlite3.connect(path / "world.db") as db:
        assert db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM opportunity_attempts").fetchone()[0] == 2
    assert run(task, MockAgent(script), judge, path, resume=True)["R_required"] == 1


def test_no_actionable_turn_before_deadline_is_excluded(tmp_path):
    task, script = scenario(deadline=5)
    judge = ScenarioJudge(lambda e, r, o: basic_decider(e, r, o))
    result = run(task, MockAgent(script), judge, tmp_path / "run")
    item = result["opportunities"][0]
    assert item["origin_tick_id"] == 2 and item["origin_turn_id"] is None
    assert item["evaluation_status"] == "unassessable" and item["final_score"] is None
    assert result["required_opportunity_count"] == 0 and result["R_required"] is None


def test_resolved_and_cancelled_are_unscored(tmp_path):
    for status in ("RESOLVED", "CANCELLED"):
        task, script = scenario()
        options = {"resolve_turn": 3} if status == "RESOLVED" else {"cancel_turn": 3}
        judge = ScenarioJudge(lambda e, r, o: basic_decider(e, r, o, **options))
        result = run(task, MockAgent(script), judge, tmp_path / status)
        assert result["opportunities"][0]["status"] == status
        assert result["opportunities"][0]["final_score"] is None
        assert result["required_opportunity_count"] == 0


def test_same_turn_independent_resolution_uses_proposal_key(tmp_path):
    task, script = scenario(silent=False)
    def decide(e, rules, opportunities):
        if e["tick_id"] == 2:
            p = proposal(actionable=True)
            return verdict(proposals=[p], settled=[Settlement(rule_id=p.rule_id, dedupe_key=p.dedupe_key,
                status="RESOLVED", reason="The Agent handled the issue independently.", evidence_refs=["world:post"])])
        return verdict()
    result = run(task, MockAgent(script), ScenarioJudge(decide), tmp_path / "run")
    assert result["opportunities"][0]["status"] == "RESOLVED"
    assert result["opportunities"][0]["final_score"] is None
    assert result["per_turn_notification_attribution"][1]["notification_evaluation"]["not_required_score"] == 1


def test_duplicate_episode_and_independent_new_episode(tmp_path):
    task, script = scenario(extra_events=[event("duplicate", 1, 10, "notification", body="Repeated upstream notice")])
    judge = ScenarioJudge(lambda e, r, o: basic_decider(e, r, o, duplicate=True))
    result = run(task, MockAgent(script), judge, tmp_path / "run")
    assert len(result["opportunities"]) == 1
    assert result["opportunities"][0]["origin_event_id"] == "change"
    # A genuinely separate cycle uses a different dedupe key even for the same rule.
    task2, script2 = scenario(extra_events=[event("new_cycle", 3, 30, "world_mutation", operation="set", arguments={"key": "signal", "value": "changed again"})])
    def decide(e, rules, opportunities):
        result = basic_decider(e, rules, opportunities)
        if any(row["id"] == "new_cycle" for row in e["triggering_events"]):
            result.new_opportunities.append(proposal(event_id="new_cycle", key="signal:order-1:cancel-cycle-2"))
        return result
    result2 = run(task2, MockAgent(script2), ScenarioJudge(decide), tmp_path / "independent")
    assert len(result2["opportunities"]) == 2


def test_one_notice_many_opportunities_and_opportunity_weighting(tmp_path):
    task, script = scenario(silent=False, second_rule=True, deadline=30, notices={2: ["covers both"]})
    def decide(e, rules, opportunities):
        if e["tick_id"] == 2:
            p1, p2 = proposal("signal", actionable=True), proposal("item", key="item:order-1:cycle-1", actionable=True)
            nid = notices_in(e)
            return verdict(proposals=[p1, p2], assessments=[
                NotificationAssessment(rule_id=p1.rule_id, dedupe_key=p1.dedupe_key, notification_ids=nid,
                    quality="complete", explanation="Complete for the first goal.", evidence_refs=["turn:notification", "world:post"]),
                NotificationAssessment(rule_id=p2.rule_id, dedupe_key=p2.dedupe_key, notification_ids=nid,
                    quality="partial", explanation="Partial for the second goal.", evidence_refs=["turn:notification", "world:post"]),
            ])
        return verdict()
    result = run(task, MockAgent(script), ScenarioJudge(decide), tmp_path / "run")
    assert [row["final_score"] for row in result["opportunities"]] == [1, 0.5]
    assert result["R_required"] == 0.75
    assert result["per_turn_notification_attribution"][1]["notification_evaluation"]["required_score"] == 0.75
    with sqlite3.connect(tmp_path / "run/world.db") as db:
        assert db.execute("SELECT COUNT(*) FROM opportunity_notifications").fetchone()[0] == 2


def test_pending_false_notice_is_penalized_once_and_harmful_is_not_double_counted(tmp_path):
    task, script = scenario(notices={3: ["unrelated", "duplicate"]})
    judge = ScenarioJudge(lambda e, r, o: basic_decider(e, r, o, false_by_turn={3}))
    result = run(task, MockAgent(script), judge, tmp_path / "false")
    assert result["turn_judgments"][2]["notification"]["false_notifications"]
    assert result["per_turn_notification_attribution"][2]["notification_evaluation"]["not_required_score"] == -1
    assert result["false_notification_count"] == 2
    task2, script2 = scenario(notices={2: ["dangerous"]})
    def harmful(e, rules, opportunities):
        result = basic_decider(e, rules, opportunities, quality_by_turn={2: "harmful"})
        if e.get("turn_id") == 2:
            result.false_notifications = notices_in(e)
        return result
    result2 = run(task2, MockAgent(script2), ScenarioJudge(harmful), tmp_path / "harmful")
    assert result2["opportunities"][0]["final_score"] == -1
    assert result2["false_notification_count"] == 0
    assert result2["turn_judgments"][1]["notification"]["false_notifications"] == []


def test_invalid_notification_id_and_duplicate_assessment_fail(tmp_path):
    task, script = scenario(notices={2: ["message"]})
    def invalid(e, rules, opportunities):
        base = basic_decider(e, rules, opportunities)
        if e.get("turn_id") == 2 and opportunities:
            base.assessments = [assessment(opportunities[0]["opportunity_id"], ["future-or-missing"], "complete")]
            base.false_notifications = []
        return base
    with pytest.raises(ValueError, match="outside the current Turn"):
        run(task, MockAgent(script), ScenarioJudge(invalid), tmp_path / "missing")
    task2, script2 = scenario(notices={2: ["message"]})
    def duplicate(e, rules, opportunities):
        base = basic_decider(e, rules, opportunities)
        if e.get("turn_id") == 2 and opportunities:
            a = assessment(opportunities[0]["opportunity_id"], notices_in(e), "partial")
            base.assessments = [a, a]
        return base
    with pytest.raises(ValueError, match="already matched"):
        run(task2, MockAgent(script2), ScenarioJudge(duplicate), tmp_path / "duplicate")


def test_historic_judge_bundle_cannot_see_future_settlement(tmp_path):
    task, script = scenario(notices={2: ["partial"], 4: ["complete"]}, deadline=150)
    judge = ScenarioJudge(lambda e, r, o: basic_decider(e, r, o, quality_by_turn={2: "partial", 4: "complete"}))
    path = tmp_path / "run"
    run(task, MockAgent(script), judge, path)
    world = SQLiteWorld(path / "world.db", task.initial_world, task.service_adapters())
    store = RunStore(world)
    loader = JudgeEvidenceLoader(path, task.judge_evidence_sources, world.history, store)
    early = json.loads((path / "evidence/turn_0001.json").read_text())
    bundle = loader.assemble(early, [], store.opportunities())
    assert bundle["active_opportunities"] == []
    second = json.loads((path / "evidence/turn_0002.json").read_text())
    bundle = loader.assemble(second, [json.loads((path / "turns/turn_0001.json").read_text())], store.opportunities())
    prior = bundle["active_opportunities"][0]
    assert prior["status"] == "PENDING" and prior["final_score"] is None
    assert len(prior["notification_attempts"]) == 1
    assert prior["notification_attempts"][0]["notifications"][0]["text"] == "partial"
    world.close()


def test_required_average_weights_opportunities_not_turns():
    turns = [{"turn_id": n, "judge_result": {"task": {"score": 0, "constraint_violations": []},
              "notification": {"false_notifications": []}, "not_required_score": 1 if n == 3 else None}} for n in (1, 2, 3)]
    opportunities = [{"opportunity_id": key, "origin_turn_id": owner, "status": "NOTIFIED", "final_score": score}
                     for key, owner, score in (("a", 1, 1), ("b", 1, 1), ("c", 2, 0))]
    report = aggregate(turns, opportunities)
    assert report["R_required"] == pytest.approx(2 / 3)
    assert report["per_turn_notification_attribution"][0]["notification_evaluation"]["required_score"] == 1
    assert report["per_turn_notification_attribution"][1]["notification_evaluation"]["required_score"] == 0
    assert report["R_notify"] == pytest.approx(0.5 * (2 / 3) + 0.5)


def test_legacy_opportunity_database_migrates(tmp_path):
    task, _ = scenario()
    world = SQLiteWorld(tmp_path / "world.db", task.initial_world, task.service_adapters())
    world.conn.executescript("""
        CREATE TABLE opportunities (opportunity_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
          trigger_event_id TEXT NOT NULL, created_at TEXT NOT NULL, related_goal TEXT NOT NULL,
          notification_deadline TEXT NOT NULL, status TEXT NOT NULL,
          notification_evidence TEXT NOT NULL, resolved_at TEXT, final_score REAL,
          reason TEXT, notification_id TEXT);
        CREATE TABLE opportunity_history (history_id INTEGER PRIMARY KEY, opportunity_id TEXT NOT NULL,
          at TEXT NOT NULL, tick_id INTEGER, turn_id INTEGER, from_status TEXT,
          to_status TEXT NOT NULL, reason TEXT NOT NULL, evidence_refs TEXT NOT NULL,
          notification_id TEXT, final_score REAL);
        CREATE TABLE opportunity_notifications (notification_id TEXT PRIMARY KEY,
          opportunity_id TEXT NOT NULL, tick_id INTEGER NOT NULL, turn_id INTEGER NOT NULL);
        INSERT INTO opportunities VALUES ('signal:change','synthetic_state_change','change',
          '2026-01-01T00:01:00+00:00','Track item.','2026-01-01T00:03:00+00:00',
          'PENDING','[]',NULL,NULL,NULL,NULL);
        INSERT INTO opportunity_history (opportunity_id,at,tick_id,turn_id,from_status,to_status,reason,evidence_refs)
          VALUES ('signal:change','2026-01-01T00:01:00+00:00',2,NULL,NULL,'OPEN','created','[]'),
                 ('signal:change','2026-01-01T00:01:10+00:00',3,2,'OPEN','PENDING','waiting','[]');
    """)
    store = RunStore(world)
    item = store.opportunities()[0]
    assert item["origin_tick_id"] == 2 and item["origin_turn_id"] == 2
    assert item["dedupe_key"] == "change"
    columns = {row["name"] for row in world.conn.execute("PRAGMA table_info(opportunity_notifications)")}
    assert {"notification_id", "opportunity_id"} <= columns
    world.close()
