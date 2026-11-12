"""每个任务运行的 Tick、Judge 和 Opportunity SQLite 记录。"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime
from typing import Any

from .world import SQLiteWorld, _history_row


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class RunStore:
    """与 World 共用同一数据库；DONE Tick 是可恢复的最小检查点。"""

    def __init__(self, world: SQLiteWorld):
        self.world = world
        self.conn = world.conn
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS ticks (
                tick_id INTEGER PRIMARY KEY, at TEXT NOT NULL, event_ids TEXT NOT NULL,
                turn_id INTEGER, record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, tick_id INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT, adapter TEXT,
                operation TEXT, arguments TEXT NOT NULL, result TEXT, changes TEXT
            );
            CREATE TABLE IF NOT EXISTS turns (
                turn_id INTEGER PRIMARY KEY, tick_id INTEGER NOT NULL,
                evidence TEXT NOT NULL, judge_result TEXT NOT NULL, evidence_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS judge_inputs (
                input_hash TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS judgments (
                judgment_id INTEGER PRIMARY KEY, tick_id INTEGER NOT NULL, turn_id INTEGER,
                kind TEXT NOT NULL, input_hash TEXT NOT NULL, judge_output TEXT NOT NULL,
                raw_output TEXT, applied_result TEXT,
                evidence_refs TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS opportunities (
                opportunity_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, trigger_event_id TEXT NOT NULL,
                created_at TEXT NOT NULL, related_goal TEXT NOT NULL, notification_deadline TEXT NOT NULL,
                status TEXT NOT NULL, notification_evidence TEXT NOT NULL, resolved_at TEXT,
                final_score REAL, reason TEXT, notification_id TEXT
            );
            CREATE TABLE IF NOT EXISTS opportunity_history (
                history_id INTEGER PRIMARY KEY, opportunity_id TEXT NOT NULL, at TEXT NOT NULL,
                tick_id INTEGER, turn_id INTEGER,
                from_status TEXT, to_status TEXT NOT NULL, reason TEXT NOT NULL,
                evidence_refs TEXT NOT NULL, notification_id TEXT, final_score REAL,
                notification_ids TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS opportunity_notifications (
                notification_id TEXT NOT NULL, opportunity_id TEXT NOT NULL,
                tick_id INTEGER NOT NULL, turn_id INTEGER NOT NULL,
                PRIMARY KEY (opportunity_id, notification_id)
            );
            CREATE TABLE IF NOT EXISTS opportunity_attempts (
                attempt_id INTEGER PRIMARY KEY, opportunity_id TEXT NOT NULL,
                tick_id INTEGER NOT NULL, turn_id INTEGER NOT NULL, at TEXT NOT NULL,
                notification_ids TEXT NOT NULL, quality TEXT NOT NULL,
                reason TEXT NOT NULL, evidence_refs TEXT NOT NULL
            );
        """)
        if "applied_result" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(judgments)")}:
            self.conn.execute("ALTER TABLE judgments ADD COLUMN applied_result TEXT")
        additions = {
            "rule_id": "TEXT", "dedupe_key": "TEXT", "origin_event_id": "TEXT",
            "origin_tick_id": "INTEGER", "origin_turn_id": "INTEGER",
            "first_actionable_at": "TEXT", "settled_tick_id": "INTEGER",
            "settled_turn_id": "INTEGER", "evaluation_status": "TEXT NOT NULL DEFAULT 'valid'",
        }
        present = {row["name"] for row in self.conn.execute("PRAGMA table_info(opportunities)")}
        for name, declaration in additions.items():
            if name not in present:
                self.conn.execute(f"ALTER TABLE opportunities ADD COLUMN {name} {declaration}")
        self.conn.execute("UPDATE opportunities SET origin_event_id=COALESCE(origin_event_id,trigger_event_id), rule_id=COALESCE(rule_id,substr(opportunity_id,1,instr(opportunity_id,':')-1)), dedupe_key=COALESCE(dedupe_key,trigger_event_id)")
        if "origin_turn_id" not in present:
            # Historic runs lacked explicit actionability. Use their first recorded
            # Agent Turn as a compatibility attribution, never for new runs.
            self.conn.execute("""UPDATE opportunities SET
                origin_tick_id=(SELECT MIN(tick_id) FROM opportunity_history h WHERE h.opportunity_id=opportunities.opportunity_id),
                origin_turn_id=(SELECT MIN(turn_id) FROM opportunity_history h WHERE h.opportunity_id=opportunities.opportunity_id AND h.turn_id IS NOT NULL),
                first_actionable_at=(SELECT MIN(at) FROM opportunity_history h WHERE h.opportunity_id=opportunities.opportunity_id AND h.turn_id IS NOT NULL),
                settled_tick_id=(SELECT tick_id FROM opportunity_history h WHERE h.opportunity_id=opportunities.opportunity_id AND h.to_status IN ('NOTIFIED','RESOLVED','CANCELLED','EXPIRED') ORDER BY h.history_id DESC LIMIT 1),
                settled_turn_id=(SELECT turn_id FROM opportunity_history h WHERE h.opportunity_id=opportunities.opportunity_id AND h.to_status IN ('NOTIFIED','RESOLVED','CANCELLED','EXPIRED') ORDER BY h.history_id DESC LIMIT 1)""")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS opportunities_episode ON opportunities(task_id,rule_id,dedupe_key)")
        notification_columns = {row["name"]: row["pk"] for row in self.conn.execute("PRAGMA table_info(opportunity_notifications)")}
        if notification_columns.get("opportunity_id") == 0:
            self.conn.executescript("""
                ALTER TABLE opportunity_notifications RENAME TO opportunity_notifications_legacy;
                CREATE TABLE opportunity_notifications (
                    notification_id TEXT NOT NULL, opportunity_id TEXT NOT NULL,
                    tick_id INTEGER NOT NULL, turn_id INTEGER NOT NULL,
                    PRIMARY KEY (opportunity_id, notification_id)
                );
                INSERT OR IGNORE INTO opportunity_notifications SELECT notification_id,opportunity_id,tick_id,turn_id FROM opportunity_notifications_legacy;
                DROP TABLE opportunity_notifications_legacy;
            """)
        if "notification_ids" not in {row["name"] for row in self.conn.execute("PRAGMA table_info(opportunity_history)")}:
            self.conn.execute("ALTER TABLE opportunity_history ADD COLUMN notification_ids TEXT NOT NULL DEFAULT '[]'")
        self.conn.execute("INSERT OR IGNORE INTO opportunity_notifications (notification_id,opportunity_id,tick_id,turn_id) SELECT o.notification_id,o.opportunity_id,COALESCE(h.tick_id,0),COALESCE(h.turn_id,0) FROM opportunities o LEFT JOIN opportunity_history h ON h.opportunity_id=o.opportunity_id AND h.notification_id=o.notification_id WHERE o.notification_id IS NOT NULL")
        self.conn.commit()

    def record_event(self, tick_id: int, ordinal: int, event, result: Any = None, changes: dict | None = None):
        with self.conn:
            self.conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (event.id, tick_id, ordinal, event.at.isoformat(), event.kind, event.body,
                               event.adapter, event.operation, _dump(event.arguments), _dump(result), _dump(changes or {})))

    def record_judgment(self, tick_id: int, turn_id: int | None, kind: str, judge_input: Any, verdict, raw_output: str | None = None, applied_result: dict | None = None):
        payload = _dump(judge_input)
        input_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO judge_inputs VALUES (?,?)", (input_hash, payload))
            self.conn.execute("INSERT INTO judgments (tick_id,turn_id,kind,input_hash,judge_output,raw_output,applied_result,evidence_refs) VALUES (?,?,?,?,?,?,?,?)",
                              (tick_id, turn_id, kind, input_hash, _dump(verdict.model_dump()), raw_output,
                               _dump(applied_result) if applied_result is not None else None, _dump(verdict.evidence_refs)))

    def notification_application(self, tick_id: int, false_ids: list[str], quiet_score: float | None = None):
        history_ids = [row[0] for row in self.conn.execute("SELECT history_id FROM opportunity_history WHERE tick_id=? ORDER BY history_id", (tick_id,))]
        return {"opportunity_history_ids": history_ids, "false_notification_ids": false_ids,
                "not_required_score": quiet_score}

    def complete_tick(self, tick: dict[str, Any], turn: dict[str, Any] | None = None, evidence_hash: str | None = None):
        """每个 Tick 完成后立即提交；未完成 Tick 的恢复不属于支持范围。"""
        with self.conn:
            if turn is not None:
                stored = {key: value for key, value in turn.items() if key not in ("judge_result", "action_trace", "tool_results")}
                stored["agent_response"] = {key: value for key, value in turn["agent_response"].items() if key != "proactive_notifications"}
                self.conn.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                                  (turn["turn_id"], tick["tick_id"], _dump(stored),
                                   _dump(turn["judge_result"]), evidence_hash))
            self.conn.execute("INSERT INTO ticks VALUES (?,?,?,?,?)",
                              (tick["tick_id"], tick["timestamp"], _dump(tick["event_ids"]),
                               turn["turn_id"] if turn else None, _dump(tick)))

    def completed_ticks(self):
        return [json.loads(row["record"]) for row in self.conn.execute("SELECT record FROM ticks ORDER BY tick_id")]

    def completed_turns(self):
        turns = []
        for row in self.conn.execute("SELECT turn_id,evidence,judge_result FROM turns ORDER BY turn_id"):
            actions = self.world.mcp_actions(row["turn_id"])
            evidence = json.loads(row["evidence"])
            evidence["agent_response"]["proactive_notifications"] = self.world.notifications(row["turn_id"])
            turns.append({**evidence, "action_trace": actions,
                          "tool_results": [action["result"] for action in actions],
                          "judge_result": json.loads(row["judge_result"])})
        return turns

    def evidence_hashes(self):
        return {f"evidence/turn_{row['turn_id']:04d}.json": row["evidence_hash"]
                for row in self.conn.execute("SELECT turn_id,evidence_hash FROM turns ORDER BY turn_id")}

    def world_changes(self, tick_id: int):
        return [_history_row(row) for row in self.conn.execute("SELECT * FROM history WHERE tick_id=? ORDER BY seq", (tick_id,))]

    def history_until(self, timestamp: str, limit: int):
        if not limit:
            return []
        now = datetime.fromisoformat(timestamp)
        selected = []
        for row in self.conn.execute("SELECT * FROM history ORDER BY seq DESC"):
            if datetime.fromisoformat(row["at"]) <= now:
                selected.append(_history_row(row))
            if len(selected) >= limit:
                break
        return list(reversed(selected))

    def changes_for(self, event_ids: list[str], turn_id: int | None, timestamp: str):
        clauses = []
        params: list[Any] = []
        if event_ids:
            clauses.append("event_id IN (" + ",".join("?" for _ in event_ids) + ")")
            params.extend(event_ids)
        if turn_id is not None:
            clauses.append("turn_id=?")
            params.append(turn_id)
        if not clauses:
            return []
        rows = self.conn.execute("SELECT * FROM history WHERE " + " OR ".join(clauses) + " ORDER BY seq", params)
        now = datetime.fromisoformat(timestamp)
        return [{key: item.get(key) for key in ("seq", "tick_id", "event_id", "turn_id", "action_id", "adapter", "operation", "changes", "patch")}
                for item in (_history_row(row) for row in rows) if datetime.fromisoformat(item["at"]) <= now]

    def events_for_ids(self, event_ids: list[str]):
        if not event_ids:
            return []
        rows = self.conn.execute("SELECT * FROM events WHERE event_id IN (" + ",".join("?" for _ in event_ids) + ")", event_ids)
        by_id = {row["event_id"]: {"event_id": row["event_id"], "tick_id": row["tick_id"], "at": row["at"],
                                   "kind": row["kind"], "body": row["body"], "adapter": row["adapter"],
                                   "operation": row["operation"], "arguments": json.loads(row["arguments"]),
                                   "result": json.loads(row["result"]), "changes": json.loads(row["changes"])} for row in rows}
        return [by_id[event_id] for event_id in event_ids]

    def event_tick_id(self, event_id: str) -> int:
        row = self.conn.execute("SELECT tick_id FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError(f"Judge cited an event that has not happened: {event_id}")
        return row["tick_id"]

    def validate_delivery(self, notification_id: str, turn_id: int) -> str:
        """Return the authoritative send time for a successful current-Turn tool call."""
        row = self.conn.execute("""SELECT n.at,n.turn_id,n.action_id,a.tool,a.success,
                   a.notification_id AS action_notification_id
                   FROM notifications n LEFT JOIN mcp_actions a ON a.action_id=n.action_id
                   WHERE n.notification_id=?""", (notification_id,)).fetchone()
        if (row is None or row["turn_id"] != turn_id or row["action_id"] is None
                or row["tool"] != "notify_user" or not row["success"]
                or row["action_notification_id"] != notification_id):
            raise ValueError(f"notification is not a successful notify_user call in current Turn: {notification_id}")
        return row["at"]

    def create_opportunity(self, item: dict[str, Any], reason: str, refs: list[str], tick_id: int | None = None, turn_id: int | None = None):
        with self.conn:
            self.conn.execute("""INSERT INTO opportunities
                (opportunity_id,task_id,trigger_event_id,created_at,related_goal,notification_deadline,status,
                 notification_evidence,resolved_at,final_score,reason,notification_id,rule_id,dedupe_key,
                 origin_event_id,origin_tick_id,origin_turn_id,first_actionable_at,settled_tick_id,settled_turn_id,evaluation_status)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (item["opportunity_id"], item["task_id"], item["trigger_event_id"], item["created_at"],
                               item["related_goal"], item["notification_deadline"], item["status"], _dump([]), None, None, None, None,
                               item["rule_id"], item["dedupe_key"], item["origin_event_id"], item["origin_tick_id"],
                               item["origin_turn_id"], item["first_actionable_at"], None, None, "valid"))
            self.conn.execute("INSERT INTO opportunity_history (opportunity_id,at,tick_id,turn_id,from_status,to_status,reason,evidence_refs) VALUES (?,?,?,?,?,?,?,?)",
                              (item["opportunity_id"], item["created_at"], tick_id, turn_id, None, "OPEN", reason, _dump(refs)))

    def update_opportunity(self, item: dict[str, Any], old_status: str, at: str, reason: str, refs: list[str], notification_ids: list[str] | None = None, tick_id: int | None = None, turn_id: int | None = None):
        notification_ids = notification_ids or []
        notification_id = item.get("notification_id") or (notification_ids[0] if notification_ids else None)
        with self.conn:
            for matched_id in notification_ids:
                self.conn.execute("INSERT INTO opportunity_notifications VALUES (?,?,?,?)", (matched_id, item["opportunity_id"], tick_id, turn_id))
            self.conn.execute("UPDATE opportunities SET status=?,notification_evidence=?,resolved_at=?,final_score=?,reason=?,notification_id=?,origin_turn_id=?,first_actionable_at=?,settled_tick_id=?,settled_turn_id=?,evaluation_status=? WHERE opportunity_id=?",
                              (item["status"], _dump(item["notification_evidence"]), item["resolved_at"], item["final_score"],
                               item["reason"], notification_id, item.get("origin_turn_id"), item.get("first_actionable_at"),
                               item.get("settled_tick_id"), item.get("settled_turn_id"), item.get("evaluation_status", "valid"), item["opportunity_id"]))
            self.conn.execute("INSERT INTO opportunity_history (opportunity_id,at,tick_id,turn_id,from_status,to_status,reason,evidence_refs,notification_id,final_score,notification_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (item["opportunity_id"], at, tick_id, turn_id, old_status, item["status"], reason, _dump(refs), notification_id, item["final_score"], _dump(item.get("notification_ids", []))))

    def record_attempt(self, item: dict[str, Any], at: str, quality: str, reason: str, refs: list[str], notification_ids: list[str], *, tick_id: int, turn_id: int):
        with self.conn:
            for notification_id in notification_ids:
                self.conn.execute("INSERT INTO opportunity_notifications VALUES (?,?,?,?)", (notification_id, item["opportunity_id"], tick_id, turn_id))
            self.conn.execute("INSERT INTO opportunity_attempts (opportunity_id,tick_id,turn_id,at,notification_ids,quality,reason,evidence_refs) VALUES (?,?,?,?,?,?,?,?)",
                              (item["opportunity_id"], tick_id, turn_id, at, _dump(notification_ids), quality, reason, _dump(refs)))
            self.conn.execute("UPDATE opportunities SET status=?,notification_id=COALESCE(notification_id,?) WHERE opportunity_id=?",
                              (item["status"], notification_ids[0], item["opportunity_id"]))
            self.conn.execute("INSERT INTO opportunity_history (opportunity_id,at,tick_id,turn_id,from_status,to_status,reason,evidence_refs,notification_id,notification_ids) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (item["opportunity_id"], at, tick_id, turn_id, item.get("previous_status", "PENDING"), item["status"], reason, _dump(refs), notification_ids[0], _dump(notification_ids)))

    def opportunities(self):
        matches = {}
        for row in self.conn.execute("SELECT opportunity_id,notification_id FROM opportunity_notifications ORDER BY rowid"):
            matches.setdefault(row["opportunity_id"], []).append(row["notification_id"])
        # Load notification content once. Prior attempts are part of the Judge's
        # cross-Turn evidence, so reconstruct them from the authoritative inbox.
        notices_by_id = {row["notification_id"]: dict(row) for row in self.conn.execute(
            "SELECT notification_id,turn_id,action_id,at,text FROM notifications")}
        attempts = {}
        for row in self.conn.execute("SELECT * FROM opportunity_attempts ORDER BY attempt_id"):
            ids = json.loads(row["notification_ids"])
            notices = []
            for notification_id in ids:
                notice = notices_by_id.get(notification_id)
                if notice is None:
                    raise ValueError(f"missing notification record: {notification_id}")
                notices.append({"notification_id": notice["notification_id"], "turn_id": notice["turn_id"], "action_id": notice["action_id"], "timestamp": notice["at"], "text": notice["text"]})
            attempts.setdefault(row["opportunity_id"], []).append({"tick_id": row["tick_id"], "turn_id": row["turn_id"], "at": row["at"], "notification_ids": ids, "notifications": notices, "quality": row["quality"], "reason": row["reason"], "evidence_refs": json.loads(row["evidence_refs"])})
        return [{**{key: row[key] for key in ("opportunity_id", "task_id", "trigger_event_id", "created_at", "related_goal",
                                            "notification_deadline", "status", "resolved_at", "final_score", "reason", "notification_id",
                                            "rule_id", "dedupe_key", "origin_event_id", "origin_tick_id", "origin_turn_id",
                                            "first_actionable_at", "settled_tick_id", "settled_turn_id", "evaluation_status")},
                 "notification_ids": matches.get(row["opportunity_id"], []), "notification_attempts": attempts.get(row["opportunity_id"], []),
                 "notification_evidence": json.loads(row["notification_evidence"])}
                for row in self.conn.execute("SELECT * FROM opportunities ORDER BY rowid")]

    def opportunity_history(self):
        return [{key: json.loads(row[key]) if key in ("evidence_refs", "notification_ids") else row[key] for key in row.keys()}
                for row in self.conn.execute("SELECT * FROM opportunity_history ORDER BY history_id")]

    def judgments(self, tick_id: int | None = None):
        query = "SELECT judgments.*,judge_inputs.payload AS judge_input FROM judgments JOIN judge_inputs USING(input_hash)" + (" WHERE tick_id=?" if tick_id is not None else "") + " ORDER BY judgment_id"
        rows = self.conn.execute(query, (tick_id,) if tick_id is not None else ())
        return [{"judgment_id": row["judgment_id"], "tick_id": row["tick_id"], "turn_id": row["turn_id"], "kind": row["kind"],
                 "judge_input": json.loads(row["judge_input"]), "judge_output": json.loads(row["judge_output"]),
                 "raw_output": row["raw_output"],
                 "applied_result": json.loads(row["applied_result"]) if row["applied_result"] else None,
                 "evidence_refs": json.loads(row["evidence_refs"])} for row in rows]
