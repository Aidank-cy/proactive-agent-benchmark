from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from .services import DomainAdapter, KVAdapter


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
        self.conn.execute("CREATE TABLE IF NOT EXISTS mcp_actions (seq INTEGER PRIMARY KEY, turn_id INTEGER, at TEXT, tool TEXT, arguments TEXT, result TEXT, success INTEGER, error TEXT, read_only INTEGER, changes TEXT, world_before TEXT, world_after TEXT)")
        if not self.conn.execute("SELECT 1 FROM state LIMIT 1").fetchone():
            for name, data in initial.items():
                if name not in adapters or not isinstance(data, dict):
                    raise ValueError(f"invalid initial adapter state: {name}")
                self.conn.execute("INSERT INTO state VALUES (?, ?)", (name, json.dumps(adapters[name].validate_state(data))))
            self.conn.commit()

    def snapshot(self):
        return {name: json.loads(data) for name, data in self.conn.execute("SELECT adapter,data FROM state ORDER BY adapter")}

    def execute(self, adapter, operation, arguments, *, mutate, source, timestamp):
        if adapter not in self.adapters:
            raise ValueError(f"unknown adapter: {adapter}")
        arguments = self.adapters[adapter].validate_arguments(operation, arguments)
        if self.adapters[adapter].is_mutating(operation) != mutate:
            raise ValueError(f"operation mode mismatch: {adapter}.{operation}")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            world_before = self.snapshot()
            state = world_before.get(adapter, {})
            result, changes = self.adapters[adapter].execute(state, operation, arguments, mutate=mutate)
            if mutate:
                state = self.adapters[adapter].validate_state(state)
                self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (adapter, json.dumps(state)))
            self.conn.execute("INSERT INTO history (at,source,adapter,operation,arguments,result,changes,world_before,world_after) VALUES (?,?,?,?,?,?,?,?,?)", (timestamp, source, adapter, operation, json.dumps(arguments), json.dumps(result), json.dumps(changes), json.dumps(world_before), json.dumps(self.snapshot())))
            self.conn.commit()
            return result, changes
        except Exception:
            self.conn.rollback()
            raise

    def history(self):
        return [dict(seq=r[0], at=r[1], source=r[2], adapter=r[3], operation=r[4], arguments=json.loads(r[5]), result=json.loads(r[6]), changes=json.loads(r[7]), world_before=json.loads(r[8]), world_after=json.loads(r[9])) for r in self.conn.execute("SELECT * FROM history ORDER BY seq")]

    def notify(self, turn_id: int, timestamp: str, content: str, opportunity_id: str | None):
        with self.conn:
            self.conn.execute("INSERT INTO notifications (turn_id,at,text,opportunity_id) VALUES (?,?,?,?)", (turn_id, timestamp, content, opportunity_id))

    def notifications(self, turn_id: int):
        return [dict(seq=r[0], timestamp=r[1], text=r[2], opportunity_id=r[3]) for r in self.conn.execute("SELECT seq,at,text,opportunity_id FROM notifications WHERE turn_id=? ORDER BY seq", (turn_id,))]

    def record_mcp_action(self, turn_id: int, timestamp: str, tool: str, arguments: dict[str, Any], result: Any, success: bool, error: str | None, changes: dict[str, Any], before: dict[str, Any], after: dict[str, Any], read_only: bool = False):
        with self.conn:
            self.conn.execute("INSERT INTO mcp_actions (turn_id,at,tool,arguments,result,success,error,read_only,changes,world_before,world_after) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (turn_id, timestamp, tool, json.dumps(arguments), json.dumps(result), int(success), error, int(read_only), json.dumps(changes), json.dumps(before), json.dumps(after)))

    def mcp_actions(self, turn_id: int):
        return [dict(seq=i, tool=r[0], arguments=json.loads(r[1]), result=json.loads(r[2]), success=bool(r[3]), error=r[4], read_only=bool(r[5]), changes=json.loads(r[6]), world_before=json.loads(r[7]), world_after=json.loads(r[8]), simulated_timestamp=r[9]) for i, r in enumerate(self.conn.execute("SELECT tool,arguments,result,success,error,read_only,changes,world_before,world_after,at FROM mcp_actions WHERE turn_id=? ORDER BY seq", (turn_id,)), 1)]

    def reset(self):
        with self.conn:
            self.conn.execute("DELETE FROM history")
            self.conn.execute("DELETE FROM notifications")
            self.conn.execute("DELETE FROM mcp_actions")
            self.conn.execute("DELETE FROM state")
            for name, data in self.initial.items():
                self.conn.execute("INSERT INTO state VALUES (?,?)", (name, json.dumps(self.adapters[name].validate_state(data))))

    def replay(self, records):
        self.reset()
        for record in records:
            adapter = record["adapter"]
            result, changes = self.execute(adapter, record["operation"], record["arguments"], mutate=self.adapters[adapter].is_mutating(record["operation"]), source=record["source"], timestamp=record["at"])
            if result != record["result"] or changes != record["changes"]:
                raise ValueError(f"replay divergence at action {record['seq']}")

    def close(self):
        self.conn.close()

