from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Protocol

from .world import SQLiteWorld


class Workspace:
    def __init__(self, path: Path, initial: dict[str, str]):
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        for name, content in initial.items():
            self.write(name, content)

    def _file(self, name):
        target = (self.path / name).resolve()
        if not target.is_relative_to(self.path.resolve()):
            raise ValueError("workspace path escape")
        return target

    def read(self, name):
        return self._file(name).read_text(encoding="utf-8")

    def write(self, name, content):
        target = self._file(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def snapshot(self):
        return {str(p.relative_to(self.path)): p.read_text(encoding="utf-8") for p in sorted(self.path.rglob("*")) if p.is_file()}


class ToolGateway:
    def __init__(self, world: SQLiteWorld, workspace: Workspace, timestamp: str, allowed: dict[str, list[str]]):
        self._world = world
        self._workspace = workspace
        self._timestamp = timestamp
        self._allowed = allowed
        self.trace: list[dict[str, Any]] = []

    def call(self, adapter: str, operation: str, **arguments):
        name = f"{adapter}.{operation}"
        seq = len(self.trace) + 1
        before = self._world.snapshot()
        try:
            if operation not in self._allowed.get(adapter, []):
                raise PermissionError(f"tool unavailable: {name}")
            result, changes = self._world.execute(adapter, operation, arguments, mutate=operation != "get", source=f"agent-action-{seq}", timestamp=self._timestamp)
            success, error = True, None
        except Exception as exc:
            result, changes, success, error = None, {}, False, str(exc)
        self.trace.append(dict(seq=seq, tool=name, arguments=copy.deepcopy(arguments), result=result, success=success, error=error, changes=changes, world_before=before, world_after=self._world.snapshot()))
        return result if success else {"error": error}

    def memory_read(self, name: str):
        return self._workspace.read(name)

    def memory_write(self, name: str, content: str):
        self._workspace.write(name, content)


class Agent(Protocol):
    def run_turn(self, observation: dict[str, Any], tools: ToolGateway, history: list[dict[str, Any]]) -> dict[str, Any]: ...


class MockAgent:
    """Deterministic test double. Scripted by turn index, never sees judge files."""
    def __init__(self, script: list[dict[str, Any]]):
        self.script = script
        self.index = 0

    def run_turn(self, observation, tools, history):
        step = self.script[self.index]
        self.index += 1
        for call in step.get("calls", []):
            tools.call(call["adapter"], call["operation"], **call.get("arguments", {}))
        for name, content in step.get("memory", {}).items():
            tools.memory_write(name, content)
        return {"text": step.get("response", ""), "proactive_notifications": step.get("proactive_notifications", [])}

