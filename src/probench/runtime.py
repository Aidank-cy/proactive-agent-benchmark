"""按模拟时间驱动事件、Agent Turn、冻结证据与 Judge 结算。"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import Agent, ToolGateway, Workspace
from .judge import Judge
from .judge_evidence import JudgeEvidenceLoader
from .notification_runtime import apply_notification_verdict
from .schema import TaskPackage
from .scoring import OpportunityTracker, aggregate
from .run_store import RunStore
from .world import SQLiteWorld, DomainAdapter


def _json(path: Path, value: Any):
    # 独占创建，避免后续运行意外覆盖已经公布的证据文件。
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False, default=str)
        f.write("\n")


def verify_evidence(run_dir: Path):
    """用发布时保存的 SHA-256 摘要检查每个 Turn 的原始证据。"""
    checks = json.loads((run_dir / "evidence_hashes.json").read_text())
    for name, digest in checks.items():
        if hashlib.sha256((run_dir / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"evidence integrity failure: {name}")
    return True


def run(task: TaskPackage, agent: Agent, judge: Judge, run_dir: Path, adapters: dict[str, DomainAdapter] | None = None, *, resume: bool = False, max_ticks: int | None = None):
    """按事件时间线运行任务，并分别产出任务分数与通知分数。

    一个时间戳对应一个 Environment Tick：先施加该时刻的世界变化，再
    决定是否唤醒 Agent。只有唤醒时才形成 Agent Turn；Turn 内可以有多个
    工具动作。每个 Turn 的前后状态与动作先冻结为证据，再交给 Judge。
    """
    if not resume and run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"run directory is not empty: {run_dir}")
    if resume and not (run_dir / "world.db").is_file():
        raise FileNotFoundError(f"no completed run database to resume: {run_dir}")
    if max_ticks is not None and max_ticks < 1:
        raise ValueError("max_ticks must be positive")
    run_dir.mkdir(parents=True, exist_ok=True)
    adapters = adapters or task.service_adapters()
    if any(binding.id not in adapters for binding in task.manifest.bindings()):
        raise ValueError("missing domain adapter")
    world = SQLiteWorld(run_dir / "world.db", task.initial_world, adapters)
    store = RunStore(world)
    judge_input_loader = JudgeEvidenceLoader(run_dir, task.judge_evidence_sources, world.history, store)
    # Judge 可读取权威后端历史；Agent 只能通过授权工具访问世界。
    if hasattr(judge, "configure"):
        judge.configure(task, run_dir, world.history)
        if getattr(judge, "loader", None) is not None:
            judge.loader.store = store
    workspace = Workspace(run_dir / "agent_workspace", task.manifest.workspace_files, initialize=not resume)
    if hasattr(agent, "start_run"):
        agent.start_run(task, run_dir)
    if not resume:
        _json(run_dir / "run_metadata.json", {"task_id": task.manifest.task_id, "agent_runtime": type(agent).__name__, "agent_model": os.environ.get("AGENT_MODEL") if getattr(agent, "external_tools", False) else None, "agent_provider_base_url": os.environ.get("AGENT_API_BASE_URL") if getattr(agent, "external_tools", False) else None, "agent_inference_parameters": {"temperature": os.environ.get("AGENT_TEMPERATURE", "0"), "context_window": os.environ.get("AGENT_CONTEXT_WINDOW", "128000")} if getattr(agent, "external_tools", False) else None, "judge_runtime": type(judge).__name__, "judge_model": os.environ.get("BENCH_JUDGE_MODEL") if type(judge).__name__ == "OpenAICompatibleJudge" else None, "judge_provider_base_url": os.environ.get("BENCH_JUDGE_BASE_URL") if type(judge).__name__ == "OpenAICompatibleJudge" else None, "judge_temperature": 0 if type(judge).__name__ == "OpenAICompatibleJudge" else None})
    tracker = OpportunityTracker(task.manifest.task_id, store)
    grouped = defaultdict(list)
    # 保留同一时间戳内的文件顺序，保证事件重放结果稳定。
    for event in task.events:
        grouped[event.at.isoformat()].append(event)
    turns = store.completed_turns() if resume else []
    ticks = store.completed_ticks() if resume else []
    if resume and len(ticks) > len(grouped):
        raise ValueError("run contains more ticks than selected task")
    if resume and json.loads((run_dir / "run_metadata.json").read_text())["task_id"] != task.manifest.task_id:
        raise ValueError("run database belongs to another task")
    if resume:
        for name, digest in store.evidence_hashes().items():
            if hashlib.sha256((run_dir / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f"evidence integrity failure: {name}")
        if turns and hasattr(agent, "resume_run"):
            agent.resume_run(len(turns))
        elif turns and not getattr(agent, "external_tools", False):
            raise ValueError("Agent runtime does not support resuming a completed Turn")
    disclosed = set()
    hashes = store.evidence_hashes() if resume else {}
    pending_events = []
    event_by_id = {event.id: event for event in task.events}
    for tick in ticks:
        for event_id in tick["event_ids"]:
            event = event_by_id[event_id]
            pending_events.append(event)
            if event.kind == "user_message":
                disclosed.add(event_id)
        if tick.get("turn_id") is not None:
            pending_events.clear()
    processed = 0
    try:
        for tick_number, (timestamp, events) in enumerate(grouped.items(), 1):
            if tick_number <= len(ticks):
                if ticks[tick_number - 1]["timestamp"] != timestamp or ticks[tick_number - 1]["event_ids"] != [event.id for event in events]:
                    raise ValueError("completed Tick does not match selected task timeline")
                continue
            tracker.reload()
            # Step 1-2：先运行环境事件。静默突变会改变世界，但不直接发给 Agent。
            tick_pre = world.snapshot()
            event_results = []
            for ordinal, event in enumerate(events, 1):
                result, changes = None, {}
                if event.kind == "world_mutation":
                    result, changes = world.execute(event.adapter, event.operation, event.arguments, mutate=True, source=f"event:{event.id}", timestamp=timestamp, tick_id=tick_number, event_id=event.id)
                    event_results.append({"event_id": event.id, "timestamp": timestamp, "affected_resources": list(changes), "result": result})
                store.record_event(tick_number, ordinal, event, result, changes)
                if event.kind == "user_message":
                    disclosed.add(event.id)
            tick = {"tick_id": tick_number, "timestamp": timestamp, "event_ids": [e.id for e in events], "turn_id": None, "mutation_results": event_results, "world_before_events": tick_pre, "world_after_events": world.snapshot()}
            ticks.append(tick)
            pending_events.extend(events)
            wake_events = [e for e in events if e.kind in ("user_message", "notification", "scheduled_wakeup")]
            if not wake_events:
                # 没有 Agent Turn 的 Tick 仍可触发/关闭通知义务，因此需要通知 Judge。
                tick_evidence = {"task_id": task.manifest.task_id, "tick_id": tick_number, "simulated_timestamp": timestamp, "triggering_events": [{"id": e.id, "kind": e.kind, "at": timestamp} for e in events], "goal_context_t": task.goal_context(disclosed), "world_pre": tick_pre, "world_post": world.snapshot(), "event_mutations": event_results, "action_trace": [], "agent_response": {"text": "", "proactive_notifications": []}}
                tick_verdict = judge.notification(tick_evidence, task.rules, tracker.values(), turns)
                tick_input = getattr(judge, "last_request", None) or judge_input_loader.assemble(tick_evidence, turns, tracker.values())
                tick_raw = getattr(judge, "last_raw_output", None)
                apply_notification_verdict(task, tracker, store, tick_verdict, events, timestamp,
                                            tick_number, None)
                tick["notification_judge"] = tick_verdict.model_dump()
                store.record_judgment(tick_number, None, "notification", tick_input, tick_verdict, tick_raw,
                                      store.notification_application(tick_number, tick_verdict.false_notifications))
                store.complete_tick(tick)
                processed += 1
                if max_ticks is not None and processed >= max_ticks:
                    return {"status": "paused", "completed_ticks": len(ticks)}
                continue
            turn_id = len(turns) + 1
            tick["turn_id"] = turn_id
            # Step 3-4：pre 是事件执行后、Agent 行动前的权威状态。
            pre = world.snapshot()
            memory_before = workspace.snapshot()
            observation = {"timestamp": timestamp, "messages": [{"event_id": e.id, "kind": e.kind, "body": e.body} for e in wake_events]}
            gateway = ToolGateway(world, workspace, timestamp, task.manifest.tool_permissions, turn_id=turn_id, tick_id=tick_number)
            if hasattr(agent, "start_turn"):
                if getattr(agent, "external_tools", False):
                    agent.start_turn(turn_id, timestamp, task.manifest.tool_permissions, tick_id=tick_number)
                else:
                    agent.start_turn(turn_id, timestamp, task.manifest.tool_permissions)
            # Step 5：Agent 自行决定工具调用数量；运行时只传当前观察与合法历史。
            response = agent.run_turn(observation, gateway, [{"observation": x["agent_observation"], "action_trace": x["action_trace"], "response": x["agent_response"]} for x in turns])
            if not isinstance(response, dict) or not isinstance(response.get("proactive_notifications", []), list):
                raise ValueError("agent must return response dict with proactive_notifications list")
            response.setdefault("proactive_notifications", [])
            # 两类 Agent 的通知都以模拟收件箱的实际发送记录为准；响应中
            # 自行声明的通知不能进入 Judge 证据。
            response["proactive_notifications"] = world.notifications(turn_id)
            gateway.trace = world.mcp_actions(turn_id)
            after_memory = workspace.snapshot()
            # Step 6-7：把 Agent 实际看到的内容、工具结果、记忆变化和 post 一起冻结。
            evidence = {"task_id": task.manifest.task_id, "turn_id": turn_id, "tick_id": tick_number, "simulated_timestamp": timestamp, "triggering_events": [{"id": e.id, "kind": e.kind, "at": e.at.isoformat()} for e in pending_events], "goal_context_t": task.goal_context(disclosed), "agent_observation": observation, "world_pre": pre, "action_trace": gateway.trace, "tool_results": [a["result"] for a in gateway.trace], "agent_response": response, "memory_before": memory_before, "memory_delta": {k: v for k, v in after_memory.items() if memory_before.get(k) != v}, "agent_session_reference": getattr(agent, "session_key", None), "agent_memory_version_before": f"{agent.session_key}:turn:{turn_id - 1}" if getattr(agent, "external_tools", False) else None, "agent_memory_version_after": f"{agent.session_key}:turn:{turn_id}" if getattr(agent, "external_tools", False) else None, "world_post": world.snapshot(), "related_opportunities": [x["opportunity_id"] for x in tracker.active()], "event_mutations": event_results}
            name = f"evidence/turn_{turn_id:04d}.json"
            _json(run_dir / name, evidence)
            hashes[name] = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
            had_active_obligation = any(datetime.fromisoformat(timestamp) <= datetime.fromisoformat(item["notification_deadline"]) for item in tracker.active())
            previous = [{k: v for k, v in t.items() if k != "judge_result"} for t in turns]
            # Step 8：任务进展与通知分别询问 Judge；工具动作本身不单独计分。
            task_result = judge.task(evidence, task.rules, previous, tracker.values())
            store.record_judgment(tick_number, turn_id, "task", getattr(judge, "last_request", None) or judge_input_loader.assemble(evidence, previous, tracker.values()), task_result, getattr(judge, "last_raw_output", None))
            notification_result = judge.notification(evidence, task.rules, tracker.values(), previous)
            notification_input = getattr(judge, "last_request", None) or judge_input_loader.assemble(evidence, previous, tracker.values())
            notification_raw_output = getattr(judge, "last_raw_output", None)
            created_ids = apply_notification_verdict(task, tracker, store, notification_result,
                                                        pending_events, timestamp, tick_number, turn_id,
                                                        response["proactive_notifications"])
            related = [o for o in tracker.values() if o["created_at"] <= timestamp and (o["resolved_at"] == timestamp or o["status"] in ("OPEN", "PENDING"))]
            # 截止前仍有待处理义务的 Turn 不计入“无需通知时保持沉默”的样本。
            created_required = any(tracker.items[oid]["status"] not in ("RESOLVED", "CANCELLED") for oid in created_ids)
            eligible_quiet = not had_active_obligation and not created_required
            false_count = len(notification_result.false_notifications)
            quiet_score = -1 if false_count else (1 if eligible_quiet else None)
            store.record_judgment(tick_number, turn_id, "notification", notification_input, notification_result,
                                  notification_raw_output,
                                  store.notification_application(tick_number, notification_result.false_notifications, quiet_score))
            turn = {**evidence, "related_opportunities": [o["opportunity_id"] for o in related], "judge_result": {"task": task_result.model_dump(), "notification": notification_result.model_dump(), "not_required_score": quiet_score}}
            turns.append(turn)
            _json(run_dir / f"turns/turn_{turn_id:04d}.json", turn)
            store.complete_tick(tick, turn, hashes[name])
            pending_events.clear()
            processed += 1
            if max_ticks is not None and processed >= max_ticks:
                return {"status": "paused", "completed_ticks": len(ticks)}
        if processed == 0 and resume and (run_dir / "report.json").is_file():
            return json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        if task.manifest.judge.get("completion_policy", "expire") == "expire":
            # 任务结束时显式结算仍未关闭的通知机会，避免遗漏统计单位。
            end = (task.manifest.initial_timestamp.timestamp() + task.manifest.duration_seconds)
            from datetime import timezone
            end_time = datetime.fromtimestamp(end, timezone.utc).isoformat()
            for item in tracker.active():
                tracker.expire(item["opportunity_id"], end_time, tick_id=len(ticks), reason="Task ended with outstanding notification obligation.")
        metrics = aggregate(turns, tracker.values())
        _json(run_dir / "ticks.json", ticks)
        _json(run_dir / "world_history.json", world.history())
        _json(run_dir / "opportunity_history.json", store.opportunity_history())
        _json(run_dir / "evidence_hashes.json", hashes)
        _json(run_dir / "report.json", metrics)
        (run_dir / "report.txt").write_text(f"Task: {task.manifest.task_id}\nR_task: {metrics['R_task']}\nR_notify: {metrics['R_notify'] if metrics['R_notify'] is not None else 'N/A'}\nTurns: {metrics['total_agent_turns']}\nTask turn scores: {metrics['task_turn_scores']}\nScored required opportunities: {metrics['required_opportunity_count']} (mean: {metrics['R_required'] if metrics['R_required'] is not None else 'N/A'})\nNot-required Turn samples: {metrics['eligible_not_required_turn_count']} (mean: {metrics['R_not_required'] if metrics['R_not_required'] is not None else 'N/A'})\nMissed notifications: {metrics['missed_notification_count']}\nFalse notifications: {metrics['false_notification_count']}\nOutstanding opportunities: {len(metrics['outstanding_opportunities'])}\nUnassessable opportunities: {len(metrics['unassessable_opportunities'])}\nCancelled opportunities: {len(metrics['cancelled_opportunities'])}\nConstraint violations: {len(metrics['constraint_violations'])}\nPer-Turn attribution and Judge details: report.json\n", encoding="utf-8")
        return metrics
    finally:
        world.close()
        if hasattr(agent, "shutdown"):
            agent.shutdown()

