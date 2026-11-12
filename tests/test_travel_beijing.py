"""Ground-truth checks for the 23-Tick, 20-Turn Beijing travel fixture."""
import json
import asyncio
import importlib.util
import sqlite3
from pathlib import Path

import pytest
import yaml

from probench.agent import MockAgent
from probench.runtime import run, verify_evidence
from probench.schema import load_task
from probench.service_plan import public_plan, plan_digest
from probench.world import SQLiteWorld

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples/travel_beijing_20turn"
_spec = importlib.util.spec_from_file_location("travel_fixture_judge", TASK / "fixture_judge.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TravelFixtureJudge = _module.TravelFixtureJudge


def setup():
    task = load_task(TASK)
    script = json.loads((TASK / "mock_agent.json").read_text())
    truth = yaml.safe_load((TASK / "ground_truth.yaml").read_text())
    return task, script, truth


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def at_path(state, dotted):
    for part in dotted.split("."):
        state = state[part]
    return state


def test_task_package_has_23_ordered_ticks_and_gradual_context():
    task, script, truth = setup()
    grouped = {}
    for event in task.events:
        grouped.setdefault(event.at.isoformat(), []).append(event)
    assert len(grouped) == truth["expected_ticks"] == 23
    assert len(script) == truth["expected_agent_turns"] == 20
    assert not any(event.kind == "scheduled_wakeup" for event in task.events)
    assert [index for index, events in enumerate(grouped.values(), 1) if all(e.kind == "world_mutation" for e in events)] == truth["silent_tick_ids"]
    assert [e.id for e in list(grouped.values())[10]] == ["location_change", "n11_location"]
    assert [e.id for e in list(grouped.values())[11]] == ["n12_route_data", "u12_goal_update"]
    assert "4800" not in json.dumps(task.goal_context({"u01_trip"}), ensure_ascii=False)
    assert "授权" not in json.dumps(task.goal_context({"u01_trip", "u02_dates_budget"}), ensure_ascii=False)
    assert "停止代为索取" not in json.dumps(task.goal_context({row.event_id for row in task.context if row.event_id != "u20_priority_cancel"}), ensure_ascii=False)
    assert "ground_truth" not in json.dumps(task.manifest.model_dump(), default=str)


def test_task_selected_mcp_tools_read_write_and_hide_judge_data(tmp_path):
    pytest.importorskip("mcp")
    from probench.world_mcp import build_mcp

    task, _, _ = setup()
    plan = public_plan(task)
    assert "ground_truth" not in json.dumps(plan)
    assert "notification_rules" not in json.dumps(plan)
    world = SQLiteWorld(tmp_path / "world.db", task.initial_world, task.service_adapters())
    world.close()
    (tmp_path / "world_control.json").write_text(json.dumps({"turn_id": 1, "tick_id": 1, "timestamp": "2026-11-18T09:00:00+08:00", "permissions": plan["permissions"], "task_id": plan["task_id"], "plan_digest": plan_digest(plan)}))
    server = build_mcp(plan, tmp_path)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == {"calendar_get", "calendar_set", "flights_get", "flights_set", "hotel_get", "hotel_set", "expenses_get", "expenses_set", "inbox_get", "transit_get", "transit_set", "notify_user"}
    assert set(tools["notify_user"].inputSchema["properties"]) == {"text"}
    assert "inbox_set" not in tools
    asyncio.run(server.call_tool("calendar_get", {"key": "client_meeting"}))
    asyncio.run(server.call_tool("calendar_set", {"key": "client_meeting", "value": {"date": "2026-11-23", "time": "09:00", "location": None, "status": "planned"}}))
    asyncio.run(server.call_tool("notify_user", {"text": "Fixture MCP delivery"}))
    world = SQLiteWorld(tmp_path / "world.db", task.initial_world, task.service_adapters())
    assert world.snapshot()["calendar"]["client_meeting"]["status"] == "planned"
    assert [row["tool"] for row in world.mcp_actions(1)] == ["calendar.get", "calendar.set", "notify_user"]
    assert len(world.notifications(1)) == 1
    world.close()


def test_full_trace_matches_every_ground_truth_tick(tmp_path):
    task, script, truth = setup()
    path = tmp_path / "run"
    result = run(task, MockAgent(script), TravelFixtureJudge(), path)
    assert result["total_agent_turns"] == truth["expected_agent_turns"]
    assert result["task_turn_scores"] == truth["expected_task_scores"]
    for key, value in truth["expected_metrics"].items():
        assert result[key] == pytest.approx(value)
    assert verify_evidence(path)
    ticks = json.loads((path / "ticks.json").read_text())
    turns = {row["tick_id"]: row for row in (json.loads(p.read_text()) for p in sorted((path / "turns").glob("*.json")))}
    assert len(ticks) == 23 and len(turns) == 20
    db = database(path / "world.db")
    assert db.execute("SELECT COUNT(*) FROM judgments").fetchone()[0] == 43
    assert db.execute("SELECT COUNT(*) FROM ticks WHERE turn_id IS NULL").fetchone()[0] == 3
    context_order = [row.event_id for row in task.context]

    for expected, tick in zip(truth["ticks"], ticks, strict=True):
        tick_id = expected["tick_id"]
        assert tick["tick_id"] == tick_id
        assert tick["event_ids"] == expected["event_ids"]
        assert tick["turn_id"] == expected["turn_id"]
        persisted_events = db.execute("SELECT event_id FROM events WHERE tick_id=? ORDER BY ordinal", (tick_id,)).fetchall()
        assert [row[0] for row in persisted_events] == expected["event_ids"]
        assert [row["event_id"] for row in tick["mutation_results"]] == expected["world_mutations"]
        transitions = db.execute("SELECT opportunity_id,to_status FROM opportunity_history WHERE tick_id=? ORDER BY history_id", (tick_id,)).fetchall()
        assert [f"{row['opportunity_id']}:{row['to_status']}" for row in transitions] == expected.get("opportunity_transition", [])

        judgment_rows = db.execute("SELECT kind,input_hash FROM judgments WHERE tick_id=? ORDER BY judgment_id", (tick_id,)).fetchall()
        assert [row["kind"] for row in judgment_rows] == (["task", "notification"] if tick_id in turns else ["notification"])
        for judgment in judgment_rows:
            bundle = json.loads(db.execute("SELECT payload FROM judge_inputs WHERE input_hash=?", (judgment["input_hash"],)).fetchone()[0])
            assert all(row["at"] <= tick["timestamp"] for row in bundle["events"])
            assert [row["event_id"] for row in bundle["goal_context"]["disclosures"]] == context_order[:context_order.index(expected["disclosed_through"]) + 1]
            assert bundle["world_state"]["post"]
            if tick_id in turns:
                assert [action["tool"] for action in bundle["actions"]] == expected["agent_tools"]
            else:
                assert bundle["actions"] == []

        if tick_id in turns:
            turn = turns[tick_id]
            assert [row["event_id"] for row in turn["agent_observation"]["messages"]] == expected["visible_event_ids"]
            assert [row["event_id"] for row in turn["goal_context_t"]["disclosures"]] == context_order[:context_order.index(expected["disclosed_through"]) + 1]
            assert [action["tool"] for action in turn["action_trace"]] == expected["agent_tools"]
            writes = [f"{action['tool'].split('.')[0]}.{key}" for action in turn["action_trace"] for key in action["changes"]]
            assert writes == expected["agent_writes"]
            assert turn["judge_result"]["task"]["category"] == expected["task_category"]
            assert len(turn["judge_result"]["notification"]["false_notifications"]) == expected.get("false_notifications", 0)
            verdict = turn["judge_result"]["notification"]
            state = turn["world_post"]
        else:
            verdict = tick["notification_judge"]
            state = tick["world_after_events"]
        case = expected["notification_case"]
        if case in ("required_created", "conditional_created", "policy_no_obligation_invoice_conditional"):
            assert verdict["new_opportunities"]
        if case == "required_notified":
            assert len(verdict["settled"]) == 1 and verdict["settled"][0]["status"] == "NOTIFIED"
        if case == "resolved_no_notification":
            assert len(verdict["settled"]) == 1 and verdict["settled"][0]["status"] == "RESOLVED"
        if case == "invoice_cancelled":
            assert len(verdict["settled"]) == 1 and verdict["settled"][0]["status"] == "CANCELLED"
        if case in ("no_obligation_route_repaired", "not_required", "duplicate_false", "unrelated_false"):
            assert not verdict["new_opportunities"] and not verdict["settled"]
        for dotted, value in expected.get("state_checks", {}).items():
            assert at_path(state, dotted) == value

    assert turns[11]["action_trace"][0]["success"] is True
    assert [action["success"] for action in turns[12]["action_trace"]] == [True, True]
    assert turns[12]["world_post"]["flights"]["outbound"]["code"] == "G112"
    assert turns[12]["judge_result"]["task"]["constraint_violations"] == ["payment_without_user_authorization"]
    assert turns[11]["world_pre"]["calendar"]["client_meeting"]["location"] == "北京望京B座"
    assert not any(row["opportunity_id"].startswith(("location_impact", "policy_impact")) for row in result["opportunities"])
    db.close()


def test_opportunity_matching_resolution_and_audit(tmp_path):
    task, script, truth = setup()
    path = tmp_path / "run"
    result = run(task, MockAgent(script), TravelFixtureJudge(), path)
    db = database(path / "world.db")
    opportunities = {row["opportunity_id"]: row for row in result["opportunities"]}
    assert set(opportunities) == set(truth["opportunities"])
    for opportunity_id, expected in truth["opportunities"].items():
        actual = opportunities[opportunity_id]
        assert actual["status"] == expected["final_status"]
        assert actual["final_score"] == expected["score"]
        histories = db.execute("SELECT tick_id,turn_id,to_status,notification_ids FROM opportunity_history WHERE opportunity_id=? ORDER BY history_id", (opportunity_id,)).fetchall()
        assert histories[0]["tick_id"] == expected["created_tick"] and histories[0]["to_status"] == "OPEN"
        assert histories[-1]["tick_id"] == expected["settled_tick"] and histories[-1]["to_status"] == expected["final_status"]
        if "pending_tick" in expected:
            assert any(row["to_status"] == "PENDING" and row["tick_id"] == expected["pending_tick"] for row in histories)
        if actual["status"] == "NOTIFIED":
            ids = actual["notification_ids"]
            assert len(ids) == 1
            assert json.loads(histories[-1]["notification_ids"]) == ids
            notification = db.execute("SELECT turn_id,action_id FROM notifications WHERE notification_id=?", (ids[0],)).fetchone()
            assert notification["turn_id"] == expected["notification_turn"]
            assert db.execute("SELECT tool,success FROM mcp_actions WHERE action_id=?", (notification["action_id"],)).fetchone()[:] == ("notify_user", 1)
            assert db.execute("SELECT opportunity_id FROM opportunity_notifications WHERE notification_id=?", (ids[0],)).fetchone()[0] == opportunity_id
        else:
            assert actual["notification_ids"] == []

    false_turns = [row["turn_id"] for row in result["turn_judgments"] if row["notification"]["false_notifications"]]
    assert false_turns == truth["false_notification_turns"]
    assert db.execute("SELECT COUNT(*) FROM opportunity_notifications").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM opportunity_history WHERE to_status IN ('NOTIFIED','RESOLVED','CANCELLED','EXPIRED')").fetchone()[0] == 5
    db.close()


def test_resume_and_short_deadline_do_not_double_settle(tmp_path):
    task, script, truth = setup()
    paused = tmp_path / "paused"
    assert run(task, MockAgent(script), TravelFixtureJudge(), paused, max_ticks=14) == {"status": "paused", "completed_ticks": 14}
    db = database(paused / "world.db")
    before = tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("ticks", "turns", "judgments", "opportunity_history"))
    assert before == (14, 12, 26, 4)
    db.close()
    resumed = run(task, MockAgent(script), TravelFixtureJudge(), paused, resume=True)
    reference = run(task, MockAgent(script), TravelFixtureJudge(), tmp_path / "reference")
    assert (resumed["R_task"], resumed["R_notify"], resumed["opportunities"]) == (reference["R_task"], reference["R_notify"], reference["opportunities"])
    assert (paused / "evidence_hashes.json").read_bytes() == (tmp_path / "reference/evidence_hashes.json").read_bytes()
    db = database(paused / "world.db")
    assert tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("ticks", "turns", "judgments", "opportunity_history")) == (23, 20, 43, 13)
    db.close()

    late_task, late_script, _ = setup()
    next(rule for rule in late_task.rules if rule.id == "return_disruption").deadline_seconds = 1800
    late = run(late_task, MockAgent(late_script), TravelFixtureJudge(), tmp_path / "late")
    return_op = next(row for row in late["opportunities"] if row["opportunity_id"].startswith("return_disruption:"))
    assert return_op["status"] == "EXPIRED" and return_op["final_score"] == 0
    assert late["missed_notification_count"] == 1
    assert late["turn_judgments"][-1]["notification"]["false_notifications"] == return_op["notification_ids"]
    assert late["R_required"] == pytest.approx(2 / 3)
