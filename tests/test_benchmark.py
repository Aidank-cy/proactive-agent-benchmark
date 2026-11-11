import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from probench.agent import MockAgent
from probench.judge import MockJudge, OpenAICompatibleJudge, TaskVerdict
from probench.runtime import run, verify_evidence
from probench.schema import Event, ContextDisclosure, load_task
from probench.scoring import OpportunityTracker, aggregate
from probench.world import KVAdapter, SQLiteWorld


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    task = load_task(ROOT / "examples/synthetic")
    script = json.loads((task.root / "mock_agent.json").read_text())
    return task, script


def execute(tmp_path, task=None, script=None):
    default_task, default_script = fixture()
    return run(task or default_task, MockAgent(script or default_script), MockJudge(), tmp_path / "run")


def test_demo_state_order_context_memory_and_evidence(tmp_path):
    result = execute(tmp_path)
    folder = tmp_path / "run"
    turns = [json.loads(p.read_text()) for p in sorted((folder / "turns").glob("*.json"))]
    assert (result["R_task"], result["R_notify"]) == (0.5, 1.0)
    assert len(turns) == 4
    assert turns[0]["world_pre"]["kv"]["item"] == 0
    assert turns[0]["world_post"]["kv"]["item"] == 1
    assert turns[1]["world_pre"]["kv"] == {"item": 1, "signal": "changed"}
    assert turns[1]["action_trace"][0]["tool"] == "kv.get"
    assert turns[1]["action_trace"][0]["result"] == "changed"
    assert turns[1]["action_trace"][0]["changes"] == {}
    assert turns[1]["world_pre"] == turns[1]["world_post"]
    assert turns[1]["memory_before"]["notes.txt"] == "item set to 1"
    assert turns[2]["memory_before"]["notes.txt"] == "item set to 1; signal changed"
    assert turns[1]["judge_result"]["not_required_score"] is None
    assert turns[3]["judge_result"]["not_required_score"] == 1
    assert result["required_opportunity_count"] == 1
    assert result["opportunities"][0]["final_score"] == 1
    assert result["opportunities"][0]["status"] == "NOTIFIED"
    assert verify_evidence(folder)
    evidence = json.loads((folder / "evidence/turn_0001.json").read_text())
    assert "judge_result" not in evidence
    assert "notification_rules" not in json.dumps(evidence)
    assert "material_signal" not in json.dumps(turns[0]["agent_observation"])


def test_future_context_and_silent_mutation(tmp_path):
    task, script = fixture()
    task.events.append(Event(id="future_user", at="2026-01-01T00:04:00+00:00", kind="user_message", body="New private preference"))
    task.context.append(ContextDisclosure(event_id="future_user", preferences=["future secret"]))
    script.append({"response": "Understood"})
    execute(tmp_path, task, script)
    folder = tmp_path / "run"
    first = json.loads((folder / "evidence/turn_0001.json").read_text())
    last = json.loads((folder / "evidence/turn_0005.json").read_text())
    assert "future secret" not in json.dumps(first)
    assert "future secret" in json.dumps(last["goal_context_t"])
    assert "future secret" not in json.dumps(last["agent_observation"])
    assert first["agent_observation"]["messages"][0]["event_id"] == "user_start"
    assert all(m["event_id"] != "change" for m in first["agent_observation"]["messages"])


def test_hidden_rules_not_in_agent_api(tmp_path):
    task, script = fixture()
    class InspectAgent(MockAgent):
        def run_turn(self, observation, tools, history):
            assert "rules" not in observation
            assert "world_pre" not in observation
            assert not hasattr(tools, "rules")
            assert "judge/notification_rules.yaml" not in tools._workspace.snapshot()
            return super().run_turn(observation, tools, history)
    run(task, InspectAgent(script), MockJudge(), tmp_path / "run")


def test_silent_tick_creates_obligation_before_later_turn(tmp_path):
    task, script = fixture()
    task.events = [e for e in task.events if e.id != "wake_check"]
    script.pop(1)
    result = execute(tmp_path, task, script)
    ticks = json.loads((tmp_path / "run/ticks.json").read_text())
    assert ticks[1]["event_ids"] == ["change"]
    assert ticks[1]["notification_judge"]["new_opportunities"][0]["trigger_event_id"] == "change"
    assert result["opportunities"][0]["created_at"] == "2026-01-01T00:01:00+00:00"
    assert result["required_opportunity_count"] == 1


def test_unauthorized_action_is_recorded(tmp_path):
    task, script = fixture()
    script[0]["calls"] = [{"adapter": "kv", "operation": "delete", "arguments": {"key": "item"}}]
    result = execute(tmp_path, task, script)
    first = json.loads((tmp_path / "run/turns/turn_0001.json").read_text())
    assert first["action_trace"][0]["success"] is False
    assert first["world_pre"] == first["world_post"]
    assert first["judge_result"]["task"]["score"] == -1
    assert result["constraint_violations"]


def test_negative_truncation_and_category_mapping():
    for category, score in [("harmful_violation", -1), ("goal_progress", 1), ("useful_information", 0.5), ("no_progress", 0)]:
        assert TaskVerdict(category=category, score=score, explanation="x", evidence_refs=["turn:response"]).score == score
    with pytest.raises(ValidationError):
        TaskVerdict(category="goal_progress", score=0.5, explanation="x", evidence_refs=["x"])
    turns = [{"judge_result": {"task": {"score": -1, "constraint_violations": []}, "not_required_score": None, "notification": {"false_notifications": []}}}]
    assert aggregate(turns, [])["R_task"] == 0


def test_notification_aggregation_empty_and_false():
    turn = lambda q, false=0: {"judge_result": {"task": {"score": 0, "constraint_violations": []}, "not_required_score": q, "notification": {"false_notifications": ["x"] * false}}}
    opportunity = lambda score: {"status": "NOTIFIED", "final_score": score}
    result = aggregate([turn(1), turn(-1, 1)], [opportunity(1), opportunity(0.5)])
    assert result["R_required"] == 0.75
    assert result["R_not_required"] == 0
    assert result["R_notify"] == 0.375
    assert result["false_notification_count"] == 1
    assert aggregate([turn(1)], [])["R_notify"] is None
    assert aggregate([turn(None)], [opportunity(1)])["R_notify"] is None


def test_tracker_settles_once_and_deadline(tmp_path):
    task, _ = fixture()
    tracker = OpportunityTracker(task.manifest.task_id)
    rule = task.rules[0]
    first = tracker.create(rule, "change", "2026-01-01T00:01:00+00:00")
    assert tracker.create(rule, "change", "2026-01-01T00:01:00+00:00") is first
    assert first["notification_deadline"] == "2026-01-01T00:03:30+00:00"
    tracker.settle(first["opportunity_id"], "NOTIFIED", 1, "2026-01-01T00:02:00+00:00", "sent", ["turn:notification"])
    with pytest.raises(ValueError, match="already settled"):
        tracker.settle(first["opportunity_id"], "NOTIFIED", 1, "2026-01-01T00:03:00+00:00", "duplicate", [])


def test_missing_and_duplicate_notifications(tmp_path):
    task, script = fixture()
    task.rules[0].deadline_seconds = 90
    script[2].pop("proactive_notifications")
    script[3]["proactive_notifications"] = [{"opportunity_id": "material_signal:change", "text": "late"}]
    result = execute(tmp_path, task, script)
    assert result["missed_notification_count"] == 1
    assert result["opportunities"][0]["final_score"] == 0
    assert result["false_notification_count"] == 1
    assert result["R_notify"] == 0
    task2, script2 = fixture()
    script2[2]["proactive_notifications"].append(script2[2]["proactive_notifications"][0].copy())
    result2 = run(task2, MockAgent(script2), MockJudge(), tmp_path / "other")
    assert result2["required_opportunity_count"] == 1
    assert result2["false_notification_count"] == 1


def test_isolation_integrity_and_replay(tmp_path):
    execute(tmp_path / "a")
    execute(tmp_path / "b")
    a = tmp_path / "a/run"
    b = tmp_path / "b/run"
    assert (a / "world_history.json").read_bytes() == (b / "world_history.json").read_bytes()
    assert (a / "evidence_hashes.json").read_bytes() == (b / "evidence_hashes.json").read_bytes()
    assert verify_evidence(a)
    (a / "evidence/turn_0001.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        verify_evidence(a)
    assert verify_evidence(b)
    world = SQLiteWorld(tmp_path / "replay.db", {"kv": {"item": 0}}, {"kv": KVAdapter()})
    world.execute("kv", "set", {"key": "item", "value": 2}, mutate=True, source="test", timestamp="2026-01-01T00:00:00+00:00")
    world.execute("kv", "get", {"key": "item"}, mutate=False, source="test", timestamp="2026-01-01T00:00:01+00:00")
    records, final = world.history(), world.snapshot()
    world.replay(records)
    assert world.snapshot() == final
    assert world.history() == records
    before = world.snapshot()
    world.record_mcp_action(1, "2026-01-01T00:00:02+00:00", "kv.delete", {"key": "item"}, None, False, "denied", {}, before, before)
    assert world.mcp_actions(1)[0]["success"] is False
    assert world.mcp_actions(1)[0]["error"] == "denied"
    world.close()


def test_judge_malformed_output(monkeypatch):
    monkeypatch.setenv("BENCH_JUDGE_API_KEY", "test")
    monkeypatch.setenv("BENCH_JUDGE_MODEL", "test")
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": '{"category":"goal_progress","score":0.5}'}}]}
    monkeypatch.setattr("probench.judge.httpx.post", lambda *a, **k: Response())
    judge = OpenAICompatibleJudge()
    with pytest.raises(ValueError, match="malformed"):
        judge.task({}, [], [], [])

