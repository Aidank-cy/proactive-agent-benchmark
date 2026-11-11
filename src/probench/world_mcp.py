"""Agent-facing MCP tools, backed by the orchestrator's SQLite world."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .world import KVAdapter, SQLiteWorld


run_name = os.environ.get("BENCH_RUN_NAME", "openclaw-demo")
if not re.fullmatch(r"[A-Za-z0-9_-]+", run_name):
    raise ValueError("BENCH_RUN_NAME must be a simple directory name")
run_dir = Path(os.environ.get("BENCH_RUNS_ROOT", "/app/runs")) / run_name
mcp = FastMCP("benchmark-world", host="0.0.0.0", port=8000, stateless_http=True, json_response=True)


def _control():
    return json.loads((run_dir / "world_control.json").read_text(encoding="utf-8"))


def _execute(operation: str, key: str, value=None):
    control = _control()
    if not (run_dir / "world.db").is_file():
        raise RuntimeError("world is not initialized")
    world = SQLiteWorld(run_dir / "world.db", {}, {"kv": KVAdapter()})
    try:
        args = {"key": key}
        if operation == "set":
            args["value"] = value
        before = world.snapshot()
        try:
            if operation not in control["permissions"].get("kv", []):
                raise PermissionError(f"kv.{operation} is not permitted")
            result, changes = world.execute("kv", operation, args, mutate=operation == "set", source="agent-mcp", timestamp=control["timestamp"])
            world.record_mcp_action(control["turn_id"], control["timestamp"], f"kv.{operation}", args, result, True, None, changes, before, world.snapshot())
            return result
        except Exception as exc:
            world.record_mcp_action(control["turn_id"], control["timestamp"], f"kv.{operation}", args, None, False, str(exc), {}, before, world.snapshot())
            raise
    finally:
        world.close()


@mcp.tool()
def world_get(key: str):
    """Read one permitted key from the simulated world; use for monitoring."""
    return _execute("get", key)


@mcp.tool()
def world_set(key: str, value: int | str | bool):
    """Set one permitted key in the simulated world."""
    return _execute("set", key, value)


@mcp.tool()
def notify_user(text: str, opportunity_id: str = ""):
    """Send a proactive notification to the simulated user inbox; never uses real messaging channels."""
    control = _control()
    world = SQLiteWorld(run_dir / "world.db", {}, {"kv": KVAdapter()})
    try:
        before = world.snapshot()
        world.notify(control["turn_id"], control["timestamp"], text, opportunity_id or None)
        result = {"delivered_to": "simulated_user_inbox", "timestamp": control["timestamp"]}
        world.record_mcp_action(control["turn_id"], control["timestamp"], "notify_user", {"text": text, "opportunity_id": opportunity_id}, result, True, None, {}, before, world.snapshot())
        return result
    finally:
        world.close()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
