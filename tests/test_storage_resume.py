import json
import sqlite3
from pathlib import Path

from probench.agent import MockAgent
from probench.judge import MockJudge
from probench.judge_evidence import JudgeEvidenceLoader
from probench.run_store import RunStore
from probench.runtime import run, verify_evidence
from probench.schema import Event, load_task
from probench.world import KVAdapter, SQLiteWorld


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    task = load_task(ROOT / "examples/synthetic")
    script = json.loads((task.root / "mock_agent.json").read_text())
    return task, script


def database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_ordered_multi_event_tick_and_compact_changes(tmp_path):
    task, script = fixture()
    task.events.insert(2, Event(id="second_change", at="2026-01-01T00:01:00+00:00", kind="world_mutation", operation="set", arguments={"key": "signal", "value": "changed_again"}))
    result = run(task, MockAgent(script), MockJudge(), tmp_path / "run")
    conn = database(tmp_path / "run/world.db")
    tick = conn.execute("SELECT * FROM ticks WHERE tick_id=2").fetchone()
    assert json.loads(tick["event_ids"]) == ["change", "second_change", "wake_check"]
    assert tick["turn_id"] == 2
    assert [row["event_id"] for row in conn.execute("SELECT event_id FROM events WHERE tick_id=2 ORDER BY ordinal")] == ["change", "second_change", "wake_check"]
    assert conn.execute("SELECT COUNT(*) FROM turns WHERE tick_id=2").fetchone()[0] == 1
    assert "world_before" not in {row["name"] for row in conn.execute("PRAGMA table_info(history)")}
    assert "world_after" not in {row["name"] for row in conn.execute("PRAGMA table_info(mcp_actions)")}
    changes = [json.loads(row["patch"]) for row in conn.execute("SELECT patch FROM history WHERE tick_id=2 ORDER BY seq")]
    assert changes[0] == [{"op": "replace", "path": "/signal", "value": "changed"}]
    assert changes[1] == [{"op": "replace", "path": "/signal", "value": "changed_again"}]
    assert result["total_agent_turns"] == 4
    conn.close()


def test_silent_tick_judgment_and_persistent_opportunity(tmp_path):
    task, script = fixture()
    task.events = [event for event in task.events if event.id != "wake_check"]
    script.pop(1)
    path = tmp_path / "run"
    paused = run(task, MockAgent(script), MockJudge(), path, max_ticks=2)
    assert paused == {"status": "paused", "completed_ticks": 2}
    conn = database(path / "world.db")
    assert conn.execute("SELECT turn_id FROM ticks WHERE tick_id=2").fetchone()[0] is None
    assert [row[0] for row in conn.execute("SELECT kind FROM judgments WHERE tick_id=2")] == ["notification"]
    assert conn.execute("SELECT status FROM opportunities").fetchone()[0] == "OPEN"
    conn.close()
    result = run(task, MockAgent(script), MockJudge(), path, resume=True)
    assert result["required_opportunity_count"] == 1
    assert result["opportunities"][0]["status"] == "NOTIFIED"


def test_resume_skips_completed_agent_and_judge_calls(tmp_path):
    task, script = fixture()
    path = tmp_path / "paused"
    run(task, MockAgent(script), MockJudge(), path, max_ticks=2)
    conn = database(path / "world.db")
    before = tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("ticks", "turns", "judgments", "mcp_actions"))
    assert before == (2, 2, 4, 2)
    conn.close()
    resumed = run(task, MockAgent(script), MockJudge(), path, resume=True)
    reference = run(task, MockAgent(script), MockJudge(), tmp_path / "reference")
    assert (resumed["R_task"], resumed["R_notify"]) == (reference["R_task"], reference["R_notify"])
    assert (path / "evidence_hashes.json").read_bytes() == (tmp_path / "reference/evidence_hashes.json").read_bytes()
    assert verify_evidence(path)
    conn = database(path / "world.db")
    assert tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("ticks", "turns", "judgments", "mcp_actions")) == (4, 4, 8, 3)
    assert json.loads(conn.execute("SELECT payload FROM judgments JOIN judge_inputs USING(input_hash) WHERE tick_id=2 AND kind='notification'").fetchone()[0])["tick"]["tick_id"] == 2
    assert conn.execute("SELECT COUNT(*) FROM judge_inputs").fetchone()[0] <= conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
    assert [row[0] for row in conn.execute("SELECT to_status FROM opportunity_history ORDER BY history_id")] == ["OPEN", "PENDING", "NOTIFIED"]
    conn.close()
    assert run(task, MockAgent(script), MockJudge(), path, resume=True)["R_task"] == resumed["R_task"]


def test_notification_ids_deadline_and_duplicate_are_persisted(tmp_path):
    task, script = fixture()
    script[2]["proactive_notifications"].append(script[2]["proactive_notifications"][0].copy())
    result = run(task, MockAgent(script), MockJudge(), tmp_path / "run")
    conn = database(tmp_path / "run/world.db")
    notifications = conn.execute("SELECT notification_id,action_id FROM notifications ORDER BY rowid").fetchall()
    assert len(notifications) == 2
    assert notifications[0]["notification_id"] != notifications[1]["notification_id"]
    assert result["turn_judgments"][2]["notification"]["false_notifications"] == [notifications[1]["notification_id"]]
    opportunity = conn.execute("SELECT notification_id,final_score FROM opportunities").fetchone()
    assert opportunity["notification_id"] == notifications[0]["notification_id"]
    assert opportunity["final_score"] == 1
    conn.close()

    late_task, late_script = fixture()
    late_task.rules[0].deadline_seconds = 90
    late_script[2].pop("proactive_notifications")
    late_result = run(late_task, MockAgent(late_script), MockJudge(), tmp_path / "late")
    assert late_result["missed_notification_count"] == 1
    late_db = database(tmp_path / "late/world.db")
    assert late_db.execute("SELECT to_status FROM opportunity_history ORDER BY history_id DESC LIMIT 1").fetchone()[0] == "EXPIRED"
    late_db.close()

    expired_task, expired_script = fixture()
    expired_task.rules[0].deadline_seconds = 30
    expired_result = run(expired_task, MockAgent(expired_script), MockJudge(), tmp_path / "expired")
    expired_db = database(tmp_path / "expired/world.db")
    late_id = expired_db.execute("SELECT notification_id FROM notifications").fetchone()[0]
    assert expired_result["turn_judgments"][2]["notification"]["false_notifications"] == [late_id]
    assert expired_db.execute("SELECT final_score FROM opportunities").fetchone()[0] == 0
    stored = json.loads(expired_db.execute("SELECT judge_output FROM judgments WHERE tick_id=3 AND kind='notification'").fetchone()[0])
    assert stored["false_notifications"] == [late_id]
    expired_db.close()


def test_patch_reconstructs_world_without_action_snapshots(tmp_path):
    world = SQLiteWorld(tmp_path / "world.db", {"kv": {"nested": {"a": 1}}}, {"kv": KVAdapter()})
    world.execute("kv", "set", {"key": "nested", "value": {"a": 2, "b": [1]}}, mutate=True, source="test", timestamp="2026-01-01T00:00:00+00:00")
    first = world.history()[-1]["seq"]
    assert world.state_at(first) == world.snapshot()
    world.execute("kv", "set", {"key": "other", "value": True}, mutate=True, source="test", timestamp="2026-01-01T00:01:00+00:00")
    assert world.state_at(first) == {"kv": {"nested": {"a": 2, "b": [1]}}}
    assert world.state_at(world.history()[-1]["seq"]) == world.snapshot()
    world.close()


def test_judge_reads_no_future_rows_from_completed_database(tmp_path):
    task, script = fixture()
    path = tmp_path / "run"
    run(task, MockAgent(script), MockJudge(), path)
    world = SQLiteWorld(path / "world.db", task.initial_world, task.service_adapters())
    store = RunStore(world)
    loader = JudgeEvidenceLoader(path, task.judge_evidence_sources, world.history, store)
    first = json.loads((path / "evidence/turn_0001.json").read_text())
    bundle = loader.assemble(first, [], [])
    assert bundle["tick"]["event_ids"] == ["user_start"]
    assert [event["event_id"] for event in bundle["events"]] == ["user_start"]
    assert all(row["event_id"] != "change" for row in bundle["world_changes"])
    assert all(row["at"] <= first["simulated_timestamp"] for row in bundle["relevant_history"]["world_history"])
    world.close()


def test_history_limit_respects_timezone_offsets(tmp_path):
    world = SQLiteWorld(tmp_path / "world.db", {"kv": {"item": 0}}, {"kv": KVAdapter()})
    store = RunStore(world)
    world.execute("kv", "set", {"key": "item", "value": 1}, mutate=True, source="first", timestamp="2026-01-01T02:00:00+02:00")
    world.execute("kv", "set", {"key": "item", "value": 2}, mutate=True, source="future", timestamp="2026-01-01T00:30:00+00:00")
    assert [row["source"] for row in store.history_until("2026-01-01T00:00:00+00:00", 10)] == ["first"]
    world.close()
