"""基于 SQLite 的权威模拟世界及 Agent 工具审计记录。"""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from .services import DomainAdapter, KVAdapter


def _state_patch(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """记录可重建状态的最小 JSON 变化；列表变化按整体替换。"""
    if isinstance(before, dict) and isinstance(after, dict):
        patch = []
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in after:
                patch.append({"op": "remove", "path": child})
            elif key not in before:
                patch.append({"op": "add", "path": child, "value": copy.deepcopy(after[key])})
            else:
                patch.extend(_state_patch(before[key], after[key], child))
        return patch
    return [{"op": "replace", "path": path, "value": copy.deepcopy(after)}] if before != after else []


def _apply_state_patch(state: Any, patch: list[dict[str, Any]]) -> Any:
    state = copy.deepcopy(state)
    for change in patch:
        keys = [part.replace("~1", "/").replace("~0", "~") for part in change["path"].split("/")[1:]]
        if not keys:
            state = copy.deepcopy(change["value"])
            continue
        parent = state
        for key in keys[:-1]:
            parent = parent[key]
        if change["op"] == "remove":
            del parent[keys[-1]]
        else:
            parent[keys[-1]] = copy.deepcopy(change["value"])
    return state


def _history_row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(seq=row["seq"], at=row["at"], source=row["source"], adapter=row["adapter"], operation=row["operation"],
                arguments=json.loads(row["arguments"]), result=json.loads(row["result"]), changes=json.loads(row["changes"]),
                patch=json.loads(row["patch"]), tick_id=row["tick_id"], event_id=row["event_id"],
                turn_id=row["turn_id"], action_id=row["action_id"])


class WorldBackend(Protocol):
    """运行时依赖的最小世界接口；具体领域行为交给 DomainAdapter。"""

    def snapshot(self) -> dict[str, Any]: ...
    def execute(self, adapter: str, operation: str, arguments: dict[str, Any], *, mutate: bool, source: str, timestamp: str) -> tuple[Any, dict[str, Any]]: ...
    def history(self) -> list[dict[str, Any]]: ...
    def reset(self) -> None: ...
    def replay(self, records: list[dict[str, Any]]) -> None: ...


class SQLiteWorld:
    """每次任务运行使用一个 SQLite 文件保存状态、操作历史和通知。"""

    def __init__(self, path: Path, initial: dict[str, Any], adapters: dict[str, DomainAdapter]):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.adapters = adapters
        self.initial = copy.deepcopy(initial)
        self.conn.execute("CREATE TABLE IF NOT EXISTS state (adapter TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS history (seq INTEGER PRIMARY KEY, at TEXT, source TEXT, adapter TEXT, operation TEXT, arguments TEXT, result TEXT, changes TEXT, patch TEXT, tick_id INTEGER, event_id TEXT, turn_id INTEGER, action_id TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS notifications (notification_id TEXT PRIMARY KEY, turn_id INTEGER, action_id TEXT, at TEXT, text TEXT, opportunity_id TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS mcp_actions (action_id TEXT PRIMARY KEY, turn_id INTEGER, at TEXT, tool TEXT, arguments TEXT, result TEXT, success INTEGER, error TEXT, read_only INTEGER, changes TEXT, notification_id TEXT)")
        # 已有数据库直接恢复状态；只有空数据库才装载任务的初始世界。
        if not self.conn.execute("SELECT 1 FROM state LIMIT 1").fetchone():
            for name, data in initial.items():
                if name not in adapters or not isinstance(data, dict):
                    raise ValueError(f"invalid initial adapter state: {name}")
                self.conn.execute("INSERT INTO state VALUES (?, ?)", (name, json.dumps(adapters[name].validate_state(data))))
            self.conn.commit()

    def snapshot(self):
        """返回可独立使用的状态副本，避免调用方直接改动 SQLite 内容。"""
        return {name: json.loads(data) for name, data in self.conn.execute("SELECT adapter,data FROM state ORDER BY adapter")}

    def execute(self, adapter, operation, arguments, *, mutate, source, timestamp, tick_id=None, event_id=None, turn_id=None, action_id=None):
        """验证并执行一次领域操作，同时原子地保存状态和权威历史。

        `mutate` 必须与服务声明的操作类型一致。返回值是工具结果与受影响
        资源的变化；只读操作也会写入历史，供后续证据和重放使用。
        """
        if adapter not in self.adapters:
            raise ValueError(f"unknown adapter: {adapter}")
        arguments = self.adapters[adapter].validate_arguments(operation, arguments)
        if self.adapters[adapter].is_mutating(operation) != mutate:
            raise ValueError(f"operation mode mismatch: {adapter}.{operation}")
        try:
            # 写锁让“读取前态 → 执行 → 写后态/历史”成为一个事务。
            self.conn.execute("BEGIN IMMEDIATE")
            before = self.snapshot().get(adapter, {})
            state = copy.deepcopy(before)
            service = self.adapters[adapter]
            if hasattr(service, "execute_at"):
                result, changes = service.execute_at(state, operation, arguments, mutate=mutate, timestamp=timestamp)
            else:
                result, changes = service.execute(state, operation, arguments, mutate=mutate)
            if mutate:
                state = self.adapters[adapter].validate_state(state)
                self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (adapter, json.dumps(state)))
            patch = _state_patch(before, state) if mutate else []
            self.conn.execute("INSERT INTO history (at,source,adapter,operation,arguments,result,changes,patch,tick_id,event_id,turn_id,action_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (timestamp, source, adapter, operation, json.dumps(arguments), json.dumps(result), json.dumps(changes), json.dumps(patch), tick_id, event_id, turn_id, action_id))
            self.conn.commit()
            return result, changes
        except Exception:
            self.conn.rollback()
            raise

    def history(self):
        return [_history_row(row) for row in self.conn.execute("SELECT * FROM history ORDER BY seq")]

    def state_at(self, seq: int):
        """从任务初态和指定序号前的 Patch 重建世界，不依赖逐动作快照。"""
        state = copy.deepcopy(self.initial)
        for row in self.conn.execute("SELECT adapter,patch FROM history WHERE seq<=? ORDER BY seq", (seq,)):
            state[row["adapter"]] = _apply_state_patch(state[row["adapter"]], json.loads(row["patch"]))
        return state

    def next_action_id(self, turn_id: int) -> str:
        seq = self.conn.execute("SELECT COUNT(*) FROM mcp_actions WHERE turn_id=?", (turn_id,)).fetchone()[0] + 1
        return f"turn:{turn_id}:action:{seq}"

    def notify(self, turn_id: int, timestamp: str, content: str, *, action_id: str | None = None, notification_id: str | None = None):
        notification_id = notification_id or f"notification:turn:{turn_id}:{self.conn.execute('SELECT COUNT(*) FROM notifications WHERE turn_id=?', (turn_id,)).fetchone()[0] + 1}"
        with self.conn:
            self.conn.execute("INSERT INTO notifications (notification_id,turn_id,action_id,at,text,opportunity_id) VALUES (?,?,?,?,?,NULL)", (notification_id, turn_id, action_id, timestamp, content))
        return notification_id

    def notifications(self, turn_id: int):
        return [dict(notification_id=r["notification_id"], action_id=r["action_id"], timestamp=r["at"], text=r["text"]) for r in self.conn.execute("SELECT * FROM notifications WHERE turn_id=? ORDER BY rowid", (turn_id,))]

    def record_mcp_action(self, turn_id: int, timestamp: str, tool: str, arguments: dict[str, Any], result: Any, success: bool, error: str | None, changes: dict[str, Any], before=None, after=None, read_only: bool = False, action_id: str | None = None, notification_id: str | None = None):
        """另存 Agent 侧 MCP 调用；失败调用不一定进入权威操作历史。"""
        action_id = action_id or self.next_action_id(turn_id)
        with self.conn:
            self.conn.execute("INSERT INTO mcp_actions (action_id,turn_id,at,tool,arguments,result,success,error,read_only,changes,notification_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (action_id, turn_id, timestamp, tool, json.dumps(arguments), json.dumps(result), int(success), error, int(read_only), json.dumps(changes), notification_id))
        return action_id

    def mcp_actions(self, turn_id: int):
        return [dict(seq=i, action_id=r["action_id"], tool=r["tool"], arguments=json.loads(r["arguments"]), result=json.loads(r["result"]), success=bool(r["success"]), error=r["error"], read_only=bool(r["read_only"]), changes=json.loads(r["changes"]), notification_id=r["notification_id"], simulated_timestamp=r["at"]) for i, r in enumerate(self.conn.execute("SELECT * FROM mcp_actions WHERE turn_id=? ORDER BY rowid", (turn_id,)), 1)]

    def reset(self):
        with self.conn:
            self.conn.execute("DELETE FROM history")
            self.conn.execute("DELETE FROM notifications")
            self.conn.execute("DELETE FROM mcp_actions")
            self.conn.execute("DELETE FROM state")
            for name, data in self.initial.items():
                self.conn.execute("INSERT INTO state VALUES (?,?)", (name, json.dumps(self.adapters[name].validate_state(data))))

    def replay(self, records):
        """从初始状态重放操作，并比较每一步的结果与变化以检测分歧。"""
        self.reset()
        for record in records:
            adapter = record["adapter"]
            result, changes = self.execute(adapter, record["operation"], record["arguments"], mutate=self.adapters[adapter].is_mutating(record["operation"]), source=record["source"], timestamp=record["at"], tick_id=record.get("tick_id"), event_id=record.get("event_id"), turn_id=record.get("turn_id"), action_id=record.get("action_id"))
            if result != record["result"] or changes != record["changes"] or self.history()[-1]["patch"] != record["patch"]:
                raise ValueError(f"replay divergence at action {record['seq']}")

    def close(self):
        self.conn.close()

