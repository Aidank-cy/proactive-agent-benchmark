"""Task-selected MCP tools backed by registered reusable services."""
from __future__ import annotations

import inspect
import json
import os
import re
from threading import Lock
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .service_plan import load_plan, plan_digest
from .services import DEFAULT_REGISTRY, ServiceRegistry, load_service_modules
from .world import SQLiteWorld


def _tool_function(name: str, description: str, input_model, callback):
    """把 Pydantic 参数模型转换成 FastMCP 能识别的动态函数签名。

    这样新增服务只需声明 OperationSpec，不必在此手写新的 MCP 工具。
    """
    parameters = []
    annotations = {}
    for field_name, field in input_model.model_fields.items():
        annotation = field.annotation or Any
        default = inspect.Parameter.empty if field.is_required() else field.default
        parameters.append(inspect.Parameter(field_name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default, annotation=annotation))
        annotations[field_name] = annotation

    def invoke(**kwargs):
        return callback(kwargs)

    invoke.__name__ = name
    invoke.__doc__ = description
    invoke.__annotations__ = annotations
    invoke.__signature__ = inspect.Signature(parameters)
    return invoke


def build_mcp(plan: dict, run_dir: Path, registry: ServiceRegistry = DEFAULT_REGISTRY) -> FastMCP:
    """仅为公开计划中获准的操作注册工具，并连接本次运行的世界数据库。"""
    if registry is DEFAULT_REGISTRY:
        load_service_modules()
    server = FastMCP("benchmark-world", host="0.0.0.0", port=8000, stateless_http=True, json_response=True)
    bindings = {item["id"]: item for item in plan["bindings"]}
    adapters = {name: registry.create(item["service"], item.get("config", {})) for name, item in bindings.items()}
    permissions = plan["permissions"]
    digest = plan_digest(plan)
    action_lock = Lock()

    def control():
        # 每次调用都读取当前 Turn 的控制文件，防止沿用上个 Turn 的权限。
        data = json.loads((run_dir / "world_control.json").read_text(encoding="utf-8"))
        if data["permissions"] != permissions or data.get("task_id") != plan["task_id"] or data.get("plan_digest") != digest:
            raise PermissionError("active turn does not match prepared service plan")
        return data

    def execute(instance: str, operation: str, arguments: dict[str, Any]):
        # 串行化 MCP 调用，确保动作顺序、前后快照与审计记录一致。
        with action_lock:
            turn = control()
            if operation not in permissions.get(instance, []):
                raise PermissionError(f"{instance}.{operation} is not permitted")
            if not (run_dir / "world.db").is_file():
                raise RuntimeError("world is not initialized")
            world = SQLiteWorld(run_dir / "world.db", {}, adapters)
            try:
                action_id = world.next_action_id(turn["turn_id"])
                try:
                    mutating = adapters[instance].is_mutating(operation)
                    result, changes = world.execute(instance, operation, arguments, mutate=mutating, source=f"action:{action_id}", timestamp=turn["timestamp"], tick_id=turn.get("tick_id"), turn_id=turn["turn_id"], action_id=action_id)
                    world.record_mcp_action(turn["turn_id"], turn["timestamp"], f"{instance}.{operation}", arguments, result, True, None, changes, read_only=not mutating, action_id=action_id)
                    return result
                except Exception as exc:
                    # 即使领域操作失败，也记录失败结果供 Judge 检查。
                    world.record_mcp_action(turn["turn_id"], turn["timestamp"], f"{instance}.{operation}", arguments, None, False, str(exc), {}, read_only=not adapters[instance].is_mutating(operation), action_id=action_id)
                    raise
            finally:
                world.close()

    names = set()
    # 工具目录由任务选择和权限共同决定，不能暴露未授权服务操作。
    for instance, item in bindings.items():
        adapter = adapters[instance]
        for tool in item["tools"]:
            operation = tool["operation"]
            name = tool["name"]
            if operation not in permissions.get(instance, []) or operation not in adapter.operations or not adapter.operations[operation].agent_visible:
                raise ValueError(f"invalid public tool: {instance}.{operation}")
            if name in names or name == "notify_user" or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid or duplicate MCP tool name: {name}")
            names.add(name)
            spec = adapter.operations[operation]
            callback = lambda args, inst=instance, op=operation: execute(inst, op, args)
            server.add_tool(_tool_function(name, spec.description, spec.input_model, callback), name=name, description=spec.description)

    def notify_user(text: str, opportunity_id: str = ""):
        """把主动通知写入模拟用户收件箱，不连接真实消息渠道。

        同时记录通知时间与 MCP 动作，使 Judge 能区别主动通知和普通回复。
        """
        with action_lock:
            turn = control()
            world = SQLiteWorld(run_dir / "world.db", {}, adapters)
            try:
                action_id = world.next_action_id(turn["turn_id"])
                notification_id = world.notify(turn["turn_id"], turn["timestamp"], text, opportunity_id or None, action_id=action_id)
                result = {"delivered_to": "simulated_user_inbox", "timestamp": turn["timestamp"], "notification_id": notification_id}
                world.record_mcp_action(turn["turn_id"], turn["timestamp"], "notify_user", {"text": text, "opportunity_id": opportunity_id}, result, True, None, {}, action_id=action_id, notification_id=notification_id)
                return result
            finally:
                world.close()

    server.add_tool(notify_user)
    return server


def main():
    run_name = os.environ.get("BENCH_RUN_NAME", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_name):
        raise ValueError("BENCH_RUN_NAME must be a simple directory name")
    plan = load_plan(Path(os.environ["BENCH_PLAN_PATH"]))
    run_dir = Path(os.environ.get("BENCH_RUNS_ROOT", "/app/runs")) / run_name
    build_mcp(plan, run_dir).run(transport="streamable-http")


if __name__ == "__main__":
    main()
