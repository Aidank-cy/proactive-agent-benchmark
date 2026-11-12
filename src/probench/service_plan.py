"""Public, Agent-facing service selection without task context or Judge files."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from .schema import TaskPackage


def public_plan(task: TaskPackage) -> dict:
    """从完整任务包中仅提取服务绑定和 Agent 获准使用的工具。"""
    adapters = task.service_adapters()
    bindings = []
    for binding in task.manifest.bindings():
        adapter = adapters[binding.id]
        bindings.append({
            "id": binding.id,
            "service": binding.service,
            "config": binding.config,
            "tools": [
                {"operation": operation, "name": binding.mcp_tool_name(operation)}
                for operation in task.manifest.tool_permissions.get(binding.id, [])
                if adapter.operations[operation].agent_visible
            ],
        })
    return {"task_id": task.manifest.task_id, "bindings": bindings, "permissions": task.manifest.tool_permissions}


def write_plan(task: TaskPackage, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(public_plan(task), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def plan_digest(plan: dict) -> str:
    # 使用规范化 JSON，使控制文件与 MCP 服务算出的摘要一致。
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_plan(task: TaskPackage, path: Path):
    if load_plan(path) != public_plan(task):
        raise ValueError("public service plan does not match selected task package")

