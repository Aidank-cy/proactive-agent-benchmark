from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol


class DomainAdapter(Protocol):
    name: str
    def is_mutating(self, operation: str) -> bool: ...
    def execute(self, state: dict[str, Any], operation: str, arguments: dict[str, Any], *, mutate: bool) -> tuple[Any, dict[str, Any]]: ...


class KVAdapter:
    """Domain-neutral fixture adapter; replace or register domain adapters for real tasks."""
    name = "kv"

    def is_mutating(self, operation: str) -> bool:
        if operation not in ("get", "set"):
            raise ValueError(f"unsupported operation: {operation}")
        return operation == "set"

    def execute(self, state, operation, arguments, *, mutate):
        key = arguments.get("key")
        if operation == "get":
            return copy.deepcopy(state.get(key)), {}
        if operation == "set" and mutate:
            before = copy.deepcopy(state.get(key))
            state[key] = copy.deepcopy(arguments["value"])
            return {"before": before, "after": state[key]}, {key: state[key]}
        raise ValueError(f"unsupported or unauthorized operation: {operation}")


class WorldBackend(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def execute(self, adapter: str, operation: str, arguments: dict[str, Any], *, mutate: bool, source: str, timestamp: str) -> tuple[Any, dict[str, Any]]: ...
    def history(self) -> list[dict[str, Any]]: ...
    def reset(self) -> None: ...
    def replay(self, records: list[dict[str, Any]]) -> None: ...


class SQLiteWorld:
    def __init__(self, path: Path, initial: dict[str, Any], adapters: dict[str, DomainAdapter]):
        self.conn = sqlite3.connect(path)
        self.adapters = adapters
        self.initial = copy.deepcopy(initial)
        self.conn.execute("CREATE TABLE IF NOT EXISTS state (adapter TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS history (seq INTEGER PRIMARY KEY, at TEXT, source TEXT, adapter TEXT, operation TEXT, arguments TEXT, result TEXT, changes TEXT, world_before TEXT, world_after TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS notifications (seq INTEGER PRIMARY KEY, turn_id INTEGER, at TEXT, text TEXT, opportunity_id TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS mcp_actions (seq INTEGER PRIMARY KEY, turn_id INTEGER, at TEXT, tool TEXT, arguments TEXT, result TEXT, success INTEGER, error TEXT, changes TEXT, world_before TEXT, world_after TEXT)")
        if not self.conn.execute("SELECT 1 FROM state LIMIT 1").fetchone():
            for name, data in initial.items():
                if name not in adapters or not isinstance(data, dict):
                    raise ValueError(f"invalid initial adapter state: {name}")
                self.conn.execute("INSERT INTO state VALUES (?, ?)", (name, json.dumps(data)))
            self.conn.commit()

    def snapshot(self):
        return {name: json.loads(data) for name, data in self.conn.execute("SELECT adapter,data FROM state ORDER BY adapter")}

    def execute(self, adapter, operation, arguments, *, mutate, source, timestamp):
        if adapter not in self.adapters:
            raise ValueError(f"unknown adapter: {adapter}")
        world_before = self.snapshot()
        state = world_before.get(adapter, {})
        result, changes = self.adapters[adapter].execute(state, operation, arguments, mutate=mutate)
        if mutate:
            with self.conn:
                self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (adapter, json.dumps(state)))
        with self.conn:
            self.conn.execute("INSERT INTO history (at,source,adapter,operation,arguments,result,changes,world_before,world_after) VALUES (?,?,?,?,?,?,?,?,?)", (timestamp, source, adapter, operation, json.dumps(arguments), json.dumps(result), json.dumps(changes), json.dumps(world_before), json.dumps(self.snapshot())))
        return result, changes

    def history(self):
        return [dict(seq=r[0], at=r[1], source=r[2], adapter=r[3], operation=r[4], arguments=json.loads(r[5]), result=json.loads(r[6]), changes=json.loads(r[7]), world_before=json.loads(r[8]), world_after=json.loads(r[9])) for r in self.conn.execute("SELECT * FROM history ORDER BY seq")]

    def notify(self, turn_id: int, timestamp: str, content: str, opportunity_id: str | None):
        with self.conn:
            self.conn.execute("INSERT INTO notifications (turn_id,at,text,opportunity_id) VALUES (?,?,?,?)", (turn_id, timestamp, content, opportunity_id))

    def notifications(self, turn_id: int):
        return [dict(seq=r[0], timestamp=r[1], text=r[2], opportunity_id=r[3]) for r in self.conn.execute("SELECT seq,at,text,opportunity_id FROM notifications WHERE turn_id=? ORDER BY seq", (turn_id,))]

    def record_mcp_action(self, turn_id: int, timestamp: str, tool: str, arguments: dict[str, Any], result: Any, success: bool, error: str | None, changes: dict[str, Any], before: dict[str, Any], after: dict[str, Any]):
        with self.conn:
            self.conn.execute("INSERT INTO mcp_actions (turn_id,at,tool,arguments,result,success,error,changes,world_before,world_after) VALUES (?,?,?,?,?,?,?,?,?,?)", (turn_id, timestamp, tool, json.dumps(arguments), json.dumps(result), int(success), error, json.dumps(changes), json.dumps(before), json.dumps(after)))

    def mcp_actions(self, turn_id: int):
        return [dict(seq=i, tool=r[0], arguments=json.loads(r[1]), result=json.loads(r[2]), success=bool(r[3]), error=r[4], changes=json.loads(r[5]), world_before=json.loads(r[6]), world_after=json.loads(r[7]), simulated_timestamp=r[8]) for i, r in enumerate(self.conn.execute("SELECT tool,arguments,result,success,error,changes,world_before,world_after,at FROM mcp_actions WHERE turn_id=? ORDER BY seq", (turn_id,)), 1)]

    def reset(self):
        with self.conn:
            self.conn.execute("DELETE FROM history")
            self.conn.execute("DELETE FROM notifications")
            self.conn.execute("DELETE FROM mcp_actions")
            self.conn.execute("DELETE FROM state")
            for name, data in self.initial.items():
                self.conn.execute("INSERT INTO state VALUES (?,?)", (name, json.dumps(data)))

    def replay(self, records):
        self.reset()
        for record in records:
            result, changes = self.execute(record["adapter"], record["operation"], record["arguments"], mutate=bool(record["changes"]), source=record["source"], timestamp=record["at"])
            if result != record["result"] or changes != record["changes"]:
                raise ValueError(f"replay divergence at action {record['seq']}")

    def close(self):
        self.conn.close()

