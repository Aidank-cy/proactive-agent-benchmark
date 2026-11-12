"""Data and orchestration checks for the unscripted, realistic travel task."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from probench.judge import MockJudge
from probench.demo_agent import InspectAgent
from probench.runtime import run, verify_evidence
from probench.schema import load_task
from probench.service_plan import write_plan
from probench.world import SQLiteWorld


TASK = Path(__file__).resolve().parents[1] / "examples" / "travel_beijing_realistic"


def test_task_shape_and_progressive_context():
    task = load_task(TASK)
    assert {name: len(records) for name, records in task.initial_world.items()} == {
        "email": 16, "calendar": 11, "flight": 9, "hotel": 6, "transit": 8, "expenses": 8,
    }
    assert Counter(e.kind for e in task.events) == {"world_mutation": 22, "user_message": 6, "notification": 9}
    assert len({e.at for e in task.events}) == 23
    assert len({e.at for e in task.events if e.kind in {"user_message", "notification"}}) == 15
    assert "4800" not in json.dumps(task.goal_context({"ev001"}), ensure_ascii=False)
    assert "付款" not in json.dumps(task.goal_context({"ev001", "ev003"}), ensure_ascii=False)
    assert "4800" in json.dumps(task.goal_context({"ev001", "ev003"}), ensure_ascii=False)
    assert "停止代为索取" not in json.dumps(task.goal_context({"ev001", "ev003", "ev007"}), ensure_ascii=False)
    assert "停止代为索取" in json.dumps(task.goal_context({"ev001", "ev003", "ev007", "ev033"}), ensure_ascii=False)
    assert not (TASK / "mock_agent.json").exists()


def test_seed_dates_and_mutations_are_consistent(tmp_path):
    task = load_task(TASK)
    seed = task.initial_world
    flight_order = seed["flight"]["air_order_261116_37"]
    assert flight_order["outbound_flight_id"] in seed["flight"]
    assert flight_order["return_flight_id"] in seed["flight"]
    assert seed["hotel"]["hotel_order_hj84"]["confirmation_email_id"] in seed["email"]
    assert seed["expenses"]["trip_costs_bj_26"]["flight_order_id"] in seed["flight"]
    assert seed["expenses"]["trip_costs_bj_26"]["hotel_order_id"] in seed["hotel"]
    assert seed["expenses"]["trip_costs_bj_26"]["total_estimate_cny"] == 2790 + 3 * 428 + 260
    for mail in task.initial_world["email"].values():
        assert datetime.fromisoformat(mail["received_at"]) <= task.manifest.initial_timestamp
    world = SQLiteWorld(tmp_path / "world.db", task.initial_world, task.service_adapters())
    for event in task.events:
        if event.kind != "world_mutation":
            continue
        if event.operation == "patch":
            assert event.arguments["id"] in world.snapshot()[event.adapter]
        if event.operation == "insert":
            record = event.arguments["record"]
            assert record["id"] not in world.snapshot()[event.adapter]
            if event.adapter == "email":
                assert datetime.fromisoformat(record["received_at"]) <= event.at
        world.execute(event.adapter, event.operation, event.arguments, mutate=True, source=f"event:{event.id}", timestamp=event.at.isoformat())
    final = world.snapshot()
    assert final["flight"]["air_seg_mu5101"]["status"] == "cancelled_by_carrier"
    assert final["flight"]["air_seg_mu5108"]["status"] == "cancelled_by_carrier"
    assert final["expenses"]["policy_domestic_q4"]["hotel_cap_per_night_cny"] == 450
    assert final["hotel"]["hotel_order_hj84"]["nightly_rate_cny"] == 428


def test_agent_surface_has_no_evaluation_labels(tmp_path):
    task = load_task(TASK)
    public = tmp_path / "public.json"
    write_plan(task, public)
    visible = json.dumps({
        "world": task.initial_world,
        "events": [{"id": e.id, "body": e.body} for e in task.events if e.kind in {"user_message", "notification"}],
        "plan": json.loads(public.read_text(encoding="utf-8")),
        "workspace": task.manifest.workspace_files,
    }, ensure_ascii=False).lower()
    for token in ("ground_truth", "expected_behavior", "expected_action", "relevant: false", "irrelevant", "noise", "notification_rules", "judge/instructions"):
        assert token not in visible
    assert all(e.id.startswith("ev") and e.id[2:].isdigit() for e in task.events)
    assert "insert" not in task.manifest.tool_permissions["email"]
    # An external record must not be part of the initial tool-visible world.
    assert "mail_019" not in task.initial_world["email"]
    for event in task.events:
        if event.kind == "user_message":
            assert not any(phrase in event.body for phrase in ("你推荐的", "你刚才问", "按照你的方案", "你已预订"))
        if event.kind == "notification":
            assert any(source in event.body for source in ("邮件", "航班", "日历", "酒店", "地图", "访客"))


def test_unscripted_agent_pipeline_smoke(tmp_path):
    task = load_task(TASK)
    result = run(task, InspectAgent(), MockJudge(), tmp_path / "run")
    assert set(("R_task", "R_notify")) <= result.keys()
    assert result["total_agent_turns"] == 15
    assert verify_evidence(tmp_path / "run")
    evidence = json.loads((tmp_path / "run" / "evidence" / "turn_0001.json").read_text())
    assert evidence["agent_observation"]["messages"][0]["event_id"] == "ev001"
    assert evidence["action_trace"][0]["tool"] == "flight.list"
    assert "mail_019" not in json.dumps(evidence["world_pre"])
    # MockJudge proves storage and orchestration only; it does not judge semantic correctness.
