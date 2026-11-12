import json
import shutil
from pathlib import Path

import pytest
import yaml

from probench.agent import MockAgent
from probench.judge import MockJudge, NotificationVerdict, OpenAICompatibleJudge, OpportunityProposal
from probench.judge_evidence import EvidenceSources, JudgeEvidenceLoader
from probench.runtime import run
from probench.schema import ContextDisclosure, Event, load_task


ROOT = Path(__file__).resolve().parents[1]


def fixture_run(tmp_path):
    task = load_task(ROOT / "tests/fixtures/synthetic")
    script = json.loads((task.root / "mock_agent.json").read_text())
    folder = tmp_path / "run"
    run(task, MockAgent(script), MockJudge(), folder)
    return task, folder


def api_judge(monkeypatch, verdict, captured):
    monkeypatch.setenv("BENCH_JUDGE_API_KEY", "test-key")
    monkeypatch.setenv("BENCH_JUDGE_MODEL", "test-model")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(verdict)}}]}

    def post(*args, **kwargs):
        captured.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("probench.judge.httpx.post", post)
    return OpenAICompatibleJudge()


def test_global_task_prompt_and_bounded_actual_evidence(monkeypatch, tmp_path):
    task, folder = fixture_run(tmp_path)
    task.judge_evidence_sources = EvidenceSources(previous_turn_limit=1, world_history_limit=1)
    captured = []
    judge = api_judge(monkeypatch, {"category": "useful_information", "score": 0.5, "explanation": "The read returned new relevant information.", "evidence_refs": ["action:1"]}, captured)
    history = json.loads((folder / "world_history.json").read_text())
    history.append({"seq": 999, "at": "2027-01-01T00:00:00+00:00", "result": "FUTURE SECRET"})
    judge.configure(task, folder, lambda: history)
    current = json.loads((folder / "evidence/turn_0002.json").read_text())
    previous = [json.loads((folder / "turns/turn_0001.json").read_text())]
    assert judge.task(current, task.rules, previous, []).score == 0.5
    request = captured[0]
    system = request["messages"][0]["content"]
    body = json.loads(request["messages"][1]["content"])
    assert "harmful_violation (-1)" in system
    assert "identify candidates only" in system
    assert "Synthetic fixture Judge guidance" in body["task_instructions"]
    assert body["goal_context"] == current["goal_context_t"]
    assert body["relevant_history"]["previous_turns"][0]["turn_id"] == 1
    assert "memory_before" not in body["relevant_history"]["previous_turns"][0]
    assert body["relevant_history"]["world_history"][-1]["seq"] != 999
    assert len(body["relevant_history"]["world_history"]) == 1
    assert "FUTURE SECRET" not in json.dumps(body)
    assert task.rules[0].id in json.dumps(body["hidden_rules"])


def test_future_turn_and_path_escape_rejected(tmp_path):
    task, folder = fixture_run(tmp_path)
    current = json.loads((folder / "evidence/turn_0002.json").read_text())
    future = json.loads((folder / "turns/turn_0003.json").read_text())
    loader = JudgeEvidenceLoader(folder, EvidenceSources(), lambda: [])
    with pytest.raises(ValueError, match="future"):
        loader.assemble(current, [future], [])
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"turn_id": 1}))
    (folder / "evidence/turn_0001.json").unlink()
    (folder / "evidence/turn_0001.json").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        loader.assemble(current, [{"turn_id": 1}], [])


def test_optional_evidence_is_selected_and_goal_changes_follow_event_order(tmp_path):
    task, folder = fixture_run(tmp_path)
    task.events.append(Event(id="later_goal", at="2026-01-01T00:04:00+00:00", kind="user_message", body="New goal"))
    task.context.insert(0, ContextDisclosure(event_id="later_goal", goals=["New goal replaces the old one."]))
    task.context.append(ContextDisclosure(event_id="user_start", goals=["Duplicate old goal note."]))
    disclosed = task.goal_context({"later_goal", "user_start"})["disclosures"]
    assert [row["event_id"] for row in disclosed] == ["user_start", "user_start", "later_goal"]
    first = task.goal_context({"user_start"})
    assert "New goal replaces" not in json.dumps(first)

    required = ["goal_context_t", "world_pre", "world_post", "triggering_events", "action_trace", "tool_results", "agent_response", "opportunities"]
    loader = JudgeEvidenceLoader(folder, EvidenceSources(sources=required), lambda: [{"seq": 999, "at": "2026-01-01T00:00:00+00:00"}])
    current = json.loads((folder / "evidence/turn_0002.json").read_text())
    payload = loader.assemble(current, [{"turn_id": 1}], [])
    assert "previous_turns" not in payload["relevant_history"]
    assert "previous_notifications" not in payload["relevant_history"]
    assert "world_history" not in payload["relevant_history"]
    assert payload["goal_context"] == current["goal_context_t"]


def test_task_judge_files_are_confined_and_legacy_defaults(tmp_path):
    root = tmp_path / "task"
    shutil.copytree(ROOT / "tests/fixtures/synthetic", root)
    manifest_path = root / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["judge_instructions"] = "context.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="judge directory"):
        load_task(root)
    manifest.pop("judge_instructions")
    manifest.pop("evidence_sources")
    manifest_path.write_text(yaml.safe_dump(manifest))
    legacy = load_task(root)
    assert legacy.judge_instructions == ""
    assert "goal_context_t" in legacy.judge_evidence_sources.sources


def test_updated_goal_can_make_candidate_event_irrelevant(tmp_path):
    task = load_task(ROOT / "tests/fixtures/synthetic")
    task.events.insert(3, Event(id="user_cancel", at="2026-01-01T00:01:00+00:00", kind="user_message", body="Stop monitoring the item."))
    task.context.append(ContextDisclosure(event_id="user_cancel", goals=["Monitoring cancelled."], interaction="User cancelled the earlier goal."))
    script = json.loads((task.root / "mock_agent.json").read_text())

    class ConditionalJudge(MockJudge):
        def notification(self, evidence, rules, opportunities, previous):
            active = evidence["goal_context_t"]["disclosures"][-1]["goals"]
            if "Monitoring cancelled." in active:
                notices = evidence["agent_response"]["proactive_notifications"]
                return NotificationVerdict(false_notifications=[n["notification_id"] for n in notices], explanation="Current user goal cancelled monitoring; candidate event is irrelevant.", evidence_refs=["goal:context"])
            return super().notification(evidence, rules, opportunities, previous)

    result = run(task, MockAgent(script), ConditionalJudge(), tmp_path / "cancelled")
    first = json.loads((tmp_path / "cancelled/evidence/turn_0001.json").read_text())
    second = json.loads((tmp_path / "cancelled/evidence/turn_0002.json").read_text())
    assert "Monitoring cancelled" not in json.dumps(first["goal_context_t"])
    assert "Monitoring cancelled" in json.dumps(second["goal_context_t"])
    assert any(e["id"] == "change" for e in second["triggering_events"])
    assert result["required_opportunity_count"] == 0
    assert result["false_notification_count"] == 1


def test_state_condition_can_create_one_cross_turn_obligation(tmp_path):
    task = load_task(ROOT / "tests/fixtures/synthetic")
    task.rules[0].trigger_event_ids = []
    script = json.loads((task.root / "mock_agent.json").read_text())

    class StateRuleJudge(MockJudge):
        def notification(self, evidence, rules, opportunities, previous):
            verdict = super().notification(evidence, rules, opportunities, previous)
            if evidence["world_post"]["kv"].get("signal") == "changed" and any(e["id"] == "wake_check" for e in evidence["triggering_events"]):
                verdict.new_opportunities.append(OpportunityProposal(rule_id=rules[0].id, trigger_event_id="wake_check", explanation="Active goal and changed signal require an update.", evidence_refs=["world:post", "goal:context"]))
            return verdict

    result = run(task, MockAgent(script), StateRuleJudge(), tmp_path / "state_rule")
    assert result["required_opportunity_count"] == 1
    assert result["opportunities"][0]["trigger_event_id"] == "wake_check"
    assert result["opportunities"][0]["status"] == "NOTIFIED"
    assert result["opportunities"][0]["final_score"] == 1


def test_structured_output_rejects_unavailable_reference(monkeypatch, tmp_path):
    task, folder = fixture_run(tmp_path)
    captured = []
    judge = api_judge(monkeypatch, {"category": "no_progress", "score": 0, "explanation": "No relevant change.", "evidence_refs": ["action:999"]}, captured)
    judge.configure(task, folder, lambda: [])
    current = json.loads((folder / "evidence/turn_0001.json").read_text())
    with pytest.raises(ValueError, match="unavailable evidence"):
        judge.task(current, task.rules, [], [])


def test_notification_candidate_is_not_automatically_required(monkeypatch, tmp_path):
    task, folder = fixture_run(tmp_path)
    captured = []
    judge = api_judge(monkeypatch, {"new_opportunities": [], "settled": [], "false_notifications": [], "explanation": "The candidate change is irrelevant to the active goal and needs no user action.", "evidence_refs": ["event:change", "goal:context"]}, captured)
    judge.configure(task, folder, lambda: [])
    current = json.loads((folder / "evidence/turn_0002.json").read_text())
    verdict = judge.notification(current, task.rules, [], [json.loads((folder / "turns/turn_0001.json").read_text())])
    assert verdict.new_opportunities == []
    system = captured[0]["messages"][0]["content"]
    body = json.loads(captured[0]["messages"][1]["content"])
    assert "notification" in system.lower()
    assert body["hidden_rules"][0]["trigger_event_ids"] == ["change"]
    assert any(event.get("event_id", event.get("id")) == "change" for event in body["events"])


def test_wrong_judge_response_type_fails_instead_of_defaulting(monkeypatch, tmp_path):
    task, folder = fixture_run(tmp_path)
    captured = []
    judge = api_judge(monkeypatch, {"category": "no_progress", "score": 0, "explanation": "Wrong kind.", "evidence_refs": ["turn:response"]}, captured)
    judge.configure(task, folder, lambda: [])
    current = json.loads((folder / "evidence/turn_0001.json").read_text())
    with pytest.raises(ValueError, match="malformed notification judge output"):
        judge.notification(current, task.rules, [], [])


def test_event_id_alone_cannot_justify_notification_opportunity(monkeypatch, tmp_path):
    task, folder = fixture_run(tmp_path)
    captured = []
    answer = {"new_opportunities": [{"rule_id": "material_signal", "trigger_event_id": "change", "explanation": "An event happened.", "evidence_refs": ["event:change"]}], "settled": [], "false_notifications": [], "explanation": "Candidate event observed.", "evidence_refs": ["event:change"]}
    judge = api_judge(monkeypatch, answer, captured)
    judge.configure(task, folder, lambda: [])
    current = json.loads((folder / "evidence/turn_0002.json").read_text())
    with pytest.raises(ValueError, match="condition evidence"):
        judge.notification(current, task.rules, [], [json.loads((folder / "turns/turn_0001.json").read_text())])
