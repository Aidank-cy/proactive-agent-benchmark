"""Agent 接口、本地工作区和用于确定性测试的工具网关。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Protocol

from .world import SQLiteWorld


class Workspace:
    """保存跨 Turn 的 Agent 文件记忆，并将访问限制在工作区内。"""

    def __init__(self, path: Path, initial: dict[str, str], *, initialize: bool = True):
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        if initialize:
            for name, content in initial.items():
                self.write(name, content)

    def _file(self, name):
        # 解析路径后再检查边界，避免通过 ../ 读取工作区外的文件。
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
    """为本地 Agent 执行授权工具调用，并记录可供 Judge 检查的动作。"""

    def __init__(self, world: SQLiteWorld, workspace: Workspace, timestamp: str, allowed: dict[str, list[str]], turn_id: int = 0, tick_id: int | None = None):
        self._world = world
        self._workspace = workspace
        self._timestamp = timestamp
        self._allowed = allowed
        self._turn_id = turn_id
        self._tick_id = tick_id
        self.trace: list[dict[str, Any]] = []

    def call(self, adapter: str, operation: str, **arguments):
        """执行一次获准的服务操作，返回工具结果并追加完整动作证据。"""
        name = f"{adapter}.{operation}"
        seq = len(self.trace) + 1
        action_id = f"turn:{self._turn_id}:action:{seq}"
        read_only = False
        try:
            if operation not in self._allowed.get(adapter, []):
                raise PermissionError(f"tool unavailable: {name}")
            read_only = not self._world.adapters[adapter].is_mutating(operation)
            result, changes = self._world.execute(adapter, operation, arguments, mutate=not read_only, source=f"action:{action_id}", timestamp=self._timestamp, tick_id=self._tick_id, turn_id=self._turn_id, action_id=action_id)
            success, error = True, None
        except Exception as exc:
            result, changes, success, error = None, {}, False, str(exc)
        # 失败调用和只读调用也要留在轨迹中，不能只记录改变世界的动作。
        self._world.record_mcp_action(self._turn_id, self._timestamp, name, arguments, result, success, error, changes, read_only=read_only, action_id=action_id)
        self.trace.append(dict(seq=seq, action_id=action_id, tool=name, arguments=copy.deepcopy(arguments), result=result, success=success, error=error, read_only=read_only, changes=changes))
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

    def resume_run(self, completed_turns: int):
        self.index = completed_turns

    def run_turn(self, observation, tools, history):
        step = self.script[self.index]
        self.index += 1
        for call in step.get("calls", []):
            tools.call(call["adapter"], call["operation"], **call.get("arguments", {}))
        for name, content in step.get("memory", {}).items():
            tools.memory_write(name, content)
        return {"text": step.get("response", ""), "proactive_notifications": step.get("proactive_notifications", [])}

