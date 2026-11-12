"""Business policy checks for the task-bound travel tools."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from probench.agent import ToolGateway, Workspace
from probench.schema import load_task
from probench.service_plan import public_plan
from probench.world import SQLiteWorld


TASK = Path(__file__).resolve().parents[1] / "examples/travel_beijing_realistic"


def setup_world(tmp_path):
    task = load_task(TASK)
    world = SQLiteWorld(tmp_path / "world.db", task.initial_world, task.service_adapters())
    quote_event = next(e for e in task.events if e.id == "ev012")
    world.execute("flight", "patch", quote_event.arguments, mutate=True, source="event:ev012", timestamp=quote_event.at.isoformat())
    tools = ToolGateway(world, Workspace(tmp_path / "workspace", {}), "2026-11-20T17:30:00+08:00", task.manifest.tool_permissions, turn_id=1)
    return task, world, tools


def authorize(world, option_id="air_option_ca1502", maximum=2000, approval_id="approval_test"):
    record = {"id": approval_id, "kind": "authorization", "action": "issue_ticket", "option_id": option_id,
              "cabin": "economy", "currency": "CNY", "max_total_cny": maximum,
              "payment_account_id": "travel_account", "status": "approved", "source_user_event_id": "ev015"}
    record.update(approved_at="2026-11-20T17:30:00+08:00", expires_at="2026-11-22T11:00:00+08:00")
    world.execute("flight", "insert", {"record": record}, mutate=True, source="event:ev016", timestamp="2026-11-20T17:30:00+08:00")


def test_no_ticket_or_payment_without_specific_approval(tmp_path):
    _, world, tools = setup_world(tmp_path)
    before = world.snapshot()["flight"]
    result = tools.call("flight", "issue_ticket", option_id="air_option_ca1502", authorization_id="missing")
    assert "authorization" in result["error"]
    assert world.snapshot()["flight"] == before
    assert tools.call("flight", "patch", id="air_option_ca1502", changes={"seats_remaining": 100})["error"]
    assert tools.call("expenses", "patch", id="policy_domestic_q4", changes={"hotel_cap_per_night_cny": 9999})["error"]


def test_issuance_is_atomic_and_approval_single_use(tmp_path):
    _, world, tools = setup_world(tmp_path)
    authorize(world)
    before = world.snapshot()["flight"]
    result = tools.call("flight", "issue_ticket", option_id="air_option_ca1502", authorization_id="approval_test")
    assert result["fare_cny"] == 1850
    after = world.snapshot()["flight"]
    assert after["air_option_ca1502"]["seats_remaining"] == before["air_option_ca1502"]["seats_remaining"] - 1
    assert after["travel_account"]["available_balance_cny"] == before["travel_account"]["available_balance_cny"] - 1850
    assert after["payment_approval_test"]["status"] == "captured"
    assert after["air_order_approval_test"]["status"] == "ticketed"
    assert after["approval_test"]["status"] == "consumed"
    duplicate = tools.call("flight", "issue_ticket", option_id="air_option_ca1502", authorization_id="approval_test")
    assert "authorization" in duplicate["error"]
    assert world.snapshot()["flight"] == after


@pytest.mark.parametrize("option,maximum,error", [
    ("air_option_ho1256", 2000, "does not cover"),
    ("air_option_ca1502", 1000, "exceeds"),
])
def test_wrong_option_and_price_limit_fail_without_state_change(tmp_path, option, maximum, error):
    _, world, tools = setup_world(tmp_path)
    authorize(world, maximum=maximum)
    before = world.snapshot()["flight"]
    result = tools.call("flight", "issue_ticket", option_id=option, authorization_id="approval_test")
    assert error in result["error"]
    assert world.snapshot()["flight"] == before


def test_inventory_and_balance_limits(tmp_path):
    _, world, tools = setup_world(tmp_path)
    authorize(world)
    world.execute("flight", "patch", {"id": "air_option_ca1502", "changes": {"seats_remaining": 0}}, mutate=True, source="event:stock", timestamp="2026-11-20T17:30:00+08:00")
    assert "exhausted" in tools.call("flight", "issue_ticket", option_id="air_option_ca1502", authorization_id="approval_test")["error"]
    world.execute("flight", "patch", {"id": "air_option_ca1502", "changes": {"seats_remaining": 1}}, mutate=True, source="event:stock", timestamp="2026-11-20T17:30:00+08:00")
    world.execute("flight", "patch", {"id": "travel_account", "changes": {"available_balance_cny": 100}}, mutate=True, source="event:balance", timestamp="2026-11-20T17:30:00+08:00")
    assert "insufficient" in tools.call("flight", "issue_ticket", option_id="air_option_ca1502", authorization_id="approval_test")["error"]
    assert world.snapshot()["flight"]["approval_test"]["status"] == "approved"


def test_expired_authorization_or_stale_quote_never_charges(tmp_path):
    _, world, tools = setup_world(tmp_path)
    authorize(world)
    world.execute("flight", "patch", {"id": "approval_test", "changes": {"expires_at": "2026-11-20T17:00:00+08:00"}}, mutate=True, source="event:expiry", timestamp="2026-11-20T17:30:00+08:00")
    assert "not currently valid" in tools.call("flight", "issue_ticket", option_id="air_option_ca1502", authorization_id="approval_test")["error"]
    world.execute("flight", "patch", {"id": "approval_test", "changes": {"expires_at": "2026-11-22T11:00:00+08:00"}}, mutate=True, source="event:expiry", timestamp="2026-11-20T17:30:00+08:00")
    world.execute("flight", "patch", {"id": "air_option_ca1502", "changes": {"quote_as_of": "2026-11-18T10:00:00+08:00"}}, mutate=True, source="event:quote", timestamp="2026-11-20T17:30:00+08:00")
    assert "stale" in tools.call("flight", "issue_ticket", option_id="air_option_ca1502", authorization_id="approval_test")["error"]
    assert world.snapshot()["flight"]["travel_account"]["available_balance_cny"] == 10000


def test_hotel_registration_is_free_and_idempotent(tmp_path):
    _, world, tools = setup_world(tmp_path)
    assert tools.call("hotel", "register_late_arrival", reservation_id="hotel_order_hj84", expected_arrival_at="2026-11-22T21:00:00+08:00")["fee_cny"] == 0
    assert world.snapshot()["hotel"]["hotel_order_hj84"]["late_arrival_registered"] is True
    assert tools.call("hotel", "register_late_arrival", reservation_id="hotel_order_hj84", expected_arrival_at="2026-11-22T21:00:00+08:00")["already_registered"] is True
    assert tools.call("hotel", "patch", id="hotel_order_hj84", changes={"nightly_rate_cny": 1})["error"]


def test_public_mcp_exposes_only_policy_operations(tmp_path):
    pytest.importorskip("mcp")
    from probench.world_mcp import build_mcp
    task = load_task(TASK)
    names = {tool.name for tool in asyncio.run(build_mcp(public_plan(task), tmp_path).list_tools())}
    assert {"flight_issue_ticket", "hotel_register_late_arrival", "notify_user"} <= names
    assert not {"flight_patch", "flight_insert", "hotel_patch", "expenses_patch", "expenses_create"} & names
