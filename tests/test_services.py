import json
import asyncio
from pathlib import Path

import pytest
import shutil
import yaml

from probench.agent import MockAgent
from probench.judge import MockJudge
from probench.runtime import run
from probench.schema import load_task
from probench.service_plan import public_plan, verify_plan, write_plan
from probench.services import CounterAdapter, DEFAULT_REGISTRY, ServiceRegistry


ROOT = Path(__file__).resolve().parents[1]


def _task(name):
    path = ROOT / "tests" / "fixtures" / name
    return load_task(path), json.loads((path / "mock_agent.json").read_text())


def test_same_registered_service_independent_task_worlds(tmp_path):
    a, a_script = _task("reuse_a")
    b, b_script = _task("reuse_b")
    assert isinstance(a.service_adapters()["counter"], CounterAdapter)
    assert isinstance(b.service_adapters()["counter"], CounterAdapter)
    assert a.service_adapters()["counter"] is not b.service_adapters()["counter"]
    run(a, MockAgent(a_script), MockJudge(), tmp_path / "a")
    run(b, MockAgent(b_script), MockJudge(), tmp_path / "b")
    a_turns = [json.loads(p.read_text()) for p in sorted((tmp_path / "a/turns").glob("*.json"))]
    b_turns = [json.loads(p.read_text()) for p in sorted((tmp_path / "b/turns").glob("*.json"))]
    assert a_turns[0]["world_pre"]["counter"]["value"] == 2
    assert b_turns[0]["world_pre"]["counter"]["value"] == 11
    assert a_turns[1]["world_pre"]["counter"]["value"] == 5
    assert b_turns[1]["world_pre"]["counter"]["value"] == 12
    assert a_turns[1]["world_post"]["counter"]["value"] == 7
    assert b_turns[1]["world_post"]["counter"]["value"] == 12
    assert a_turns[0]["action_trace"][0]["read_only"] is True
    assert a_turns[1]["action_trace"][0]["read_only"] is False


def test_service_plan_and_permissions(tmp_path):
    a, _ = _task("reuse_a")
    b, script = _task("reuse_b")
    a_plan = public_plan(a)
    b_plan = public_plan(b)
    assert {x["name"] for x in a_plan["bindings"][0]["tools"]} == {"counter_read", "counter_increment"}
    assert {x["name"] for x in b_plan["bindings"][0]["tools"]} == {"counter_read"}
    assert "notification_rules" not in json.dumps(a_plan)
    path = tmp_path / "plan.json"
    write_plan(a, path)
    verify_plan(a, path)
    with pytest.raises(ValueError, match="does not match"):
        verify_plan(b, path)
    script[0]["calls"] = [{"adapter": "counter", "operation": "increment", "arguments": {"amount": 1}}]
    run(b, MockAgent(script), MockJudge(), tmp_path / "restricted")
    first = json.loads((tmp_path / "restricted/turns/turn_0001.json").read_text())
    assert first["action_trace"][0]["success"] is False
    assert first["world_post"]["counter"]["value"] == 11


def test_registry_rejects_duplicate_and_invalid_state(tmp_path):
    registry = ServiceRegistry()
    registry.register("counter", CounterAdapter)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("counter", CounterAdapter)
    with pytest.raises(ValueError, match="unknown service"):
        registry.create("weather")
    with pytest.raises(Exception):
        CounterAdapter().validate_state({"value": -1})
    with pytest.raises(Exception):
        CounterAdapter().validate_arguments("increment", {"amount": 0})


def test_task_can_bind_multiple_services_without_new_service_code(tmp_path):
    source = ROOT / "tests/fixtures/reuse_a"
    target = tmp_path / "combined"
    shutil.copytree(source, target)
    manifest = yaml.safe_load((target / "manifest.yaml").read_text())
    manifest["services"].append({"id": "notes", "service": "kv"})
    manifest["tool_permissions"]["notes"] = ["get"]
    (target / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    world = yaml.safe_load((target / "initial_world.yaml").read_text())
    world["notes"] = {"message": "synthetic"}
    (target / "initial_world.yaml").write_text(yaml.safe_dump(world))
    task = load_task(target)
    assert set(task.service_adapters()) == {"counter", "notes"}
    assert {x["name"] for binding in public_plan(task)["bindings"] for x in binding["tools"]} == {"counter_read", "counter_increment", "notes_get"}


def test_mcp_registration_reflects_selected_permissions(tmp_path):
    pytest.importorskip("mcp")
    from probench.world_mcp import build_mcp
    a, _ = _task("reuse_a")
    b, _ = _task("reuse_b")
    names_a = {tool.name for tool in asyncio.run(build_mcp(public_plan(a), tmp_path).list_tools())}
    names_b = {tool.name for tool in asyncio.run(build_mcp(public_plan(b), tmp_path).list_tools())}
    assert names_a == {"counter_read", "counter_increment", "notify_user"}
    assert names_b == {"counter_read", "notify_user"}
    old, _ = _task("synthetic")
    names_old = {tool.name for tool in asyncio.run(build_mcp(public_plan(old), tmp_path).list_tools())}
    assert names_old == {"world_get", "world_set", "notify_user"}
