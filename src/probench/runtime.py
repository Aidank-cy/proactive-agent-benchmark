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
from .schema import TaskPackage
from .scoring import OpportunityTracker, aggregate
from .world import KVAdapter, SQLiteWorld, DomainAdapter


def _json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False, default=str)
        f.write("\n")


def verify_evidence(run_dir: Path):
    checks = json.loads((run_dir / "evidence_hashes.json").read_text())
    for name, digest in checks.items():
        if hashlib.sha256((run_dir / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"evidence integrity failure: {name}")
    return True


def run(task: TaskPackage, agent: Agent, judge: Judge, run_dir: Path, adapters: dict[str, DomainAdapter] | None = None):
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    adapters = adapters or {"kv": KVAdapter()}
    if any(name not in adapters for name in task.manifest.adapters):
        raise ValueError("missing domain adapter")
    world = SQLiteWorld(run_dir / "world.db", task.initial_world, adapters)
    workspace = Workspace(run_dir / "agent_workspace", task.manifest.workspace_files)
    if hasattr(agent, "start_run"):
        agent.start_run(task, run_dir)
    _json(run_dir / "run_metadata.json", {"task_id": task.manifest.task_id, "agent_runtime": type(agent).__name__, "agent_model": os.environ.get("AGENT_MODEL") if getattr(agent, "external_tools", False) else None, "agent_provider_base_url": os.environ.get("AGENT_API_BASE_URL") if getattr(agent, "external_tools", False) else None, "agent_inference_parameters": {"temperature": os.environ.get("AGENT_TEMPERATURE", "0"), "context_window": os.environ.get("AGENT_CONTEXT_WINDOW", "128000")} if getattr(agent, "external_tools", False) else None, "judge_runtime": type(judge).__name__, "judge_model": os.environ.get("BENCH_JUDGE_MODEL") if type(judge).__name__ == "OpenAICompatibleJudge" else None, "judge_provider_base_url": os.environ.get("BENCH_JUDGE_BASE_URL") if type(judge).__name__ == "OpenAICompatibleJudge" else None, "judge_temperature": 0 if type(judge).__name__ == "OpenAICompatibleJudge" else None})
    tracker = OpportunityTracker(task.manifest.task_id)
    grouped = defaultdict(list)
    for event in task.events:
        grouped[event.at.isoformat()].append(event)
    turns = []
    ticks = []
    disclosed = set()
    hashes = {}
    pending_events = []
    try:
        for tick_number, (timestamp, events) in enumerate(grouped.items(), 1):
            tick_pre = world.snapshot()
            event_results = []
            for event in events:
                if event.kind == "world_mutation":
                    result, changes = world.execute(event.adapter, event.operation, event.arguments, mutate=True, source=f"event:{event.id}", timestamp=timestamp)
                    event_results.append({"event_id": event.id, "timestamp": timestamp, "affected_resources": list(changes), "result": result})
                if event.kind == "user_message":
                    disclosed.add(event.id)
            tick = {"tick_id": tick_number, "timestamp": timestamp, "event_ids": [e.id for e in events], "mutation_results": event_results, "world_before_events": tick_pre, "world_after_events": world.snapshot()}
            ticks.append(tick)
            pending_events.extend(events)
            wake_events = [e for e in events if e.kind in ("user_message", "notification", "scheduled_wakeup")]
            if not wake_events:
                tick_evidence = {"task_id": task.manifest.task_id, "tick_id": tick_number, "simulated_timestamp": timestamp, "triggering_events": [{"id": e.id, "kind": e.kind, "at": timestamp} for e in events], "goal_context_t": task.goal_context(disclosed), "world_pre": tick_pre, "world_post": world.snapshot(), "event_mutations": event_results, "action_trace": [], "agent_response": {"text": "", "proactive_notifications": []}}
                tick_verdict = judge.notification(tick_evidence, task.rules, tracker.values(), turns)
                tick["notification_judge"] = tick_verdict.model_dump()
                for proposal in tick_verdict.new_opportunities:
                    rule = next((r for r in task.rules if r.id == proposal.rule_id), None)
                    if rule is None or proposal.trigger_event_id not in {e.id for e in events} or proposal.trigger_event_id not in rule.trigger_event_ids:
                        raise ValueError("judge proposed unknown rule or trigger event")
                    tracker.create(rule, proposal.trigger_event_id, timestamp)
                for settlement in tick_verdict.settled:
                    if settlement.status not in ("RESOLVED", "CANCELLED"):
                        raise ValueError("notification cannot be scored without an Agent Turn")
                    tracker.settle(settlement.opportunity_id, settlement.status, settlement.score, timestamp, settlement.reason, settlement.evidence_refs)
                for item in tracker.active():
                    if datetime.fromisoformat(timestamp) > datetime.fromisoformat(item["notification_deadline"]):
                        tracker.settle(item["opportunity_id"], "EXPIRED", 0, timestamp, "Notification deadline passed.", ["tick:clock"])
                continue
            turn_id = len(turns) + 1
            pre = world.snapshot()
            memory_before = workspace.snapshot()
            observation = {"timestamp": timestamp, "messages": [{"event_id": e.id, "kind": e.kind, "body": e.body} for e in wake_events]}
            gateway = ToolGateway(world, workspace, timestamp, task.manifest.tool_permissions)
            if hasattr(agent, "start_turn"):
                agent.start_turn(turn_id, timestamp, task.manifest.tool_permissions)
            response = agent.run_turn(observation, gateway, [{"observation": x["agent_observation"], "action_trace": x["action_trace"], "response": x["agent_response"]} for x in turns])
            if not isinstance(response, dict) or not isinstance(response.get("proactive_notifications", []), list):
                raise ValueError("agent must return response dict with proactive_notifications list")
            response.setdefault("proactive_notifications", [])
            if getattr(agent, "external_tools", False):
                gateway.trace = world.mcp_actions(turn_id)
                response["proactive_notifications"] = world.notifications(turn_id)
            after_memory = workspace.snapshot()
            evidence = {"task_id": task.manifest.task_id, "turn_id": turn_id, "tick_id": tick_number, "simulated_timestamp": timestamp, "triggering_events": [{"id": e.id, "kind": e.kind, "at": e.at.isoformat()} for e in pending_events], "goal_context_t": task.goal_context(disclosed), "agent_observation": observation, "world_pre": pre, "action_trace": gateway.trace, "tool_results": [a["result"] for a in gateway.trace], "agent_response": response, "memory_before": memory_before, "memory_delta": {k: v for k, v in after_memory.items() if memory_before.get(k) != v}, "agent_session_reference": getattr(agent, "session_key", None), "agent_memory_version_before": f"{agent.session_key}:turn:{turn_id - 1}" if getattr(agent, "external_tools", False) else None, "agent_memory_version_after": f"{agent.session_key}:turn:{turn_id}" if getattr(agent, "external_tools", False) else None, "world_post": world.snapshot(), "related_opportunities": [x["opportunity_id"] for x in tracker.active()], "event_mutations": event_results}
            name = f"evidence/turn_{turn_id:04d}.json"
            _json(run_dir / name, evidence)
            hashes[name] = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
            for item in tracker.active():
                if datetime.fromisoformat(timestamp) > datetime.fromisoformat(item["notification_deadline"]):
                    tracker.settle(item["opportunity_id"], "EXPIRED", 0, timestamp, "Notification deadline passed.", ["tick:clock"])
            had_active_obligation = bool(tracker.active())
            previous = [{k: v for k, v in t.items() if k != "judge_result"} for t in turns]
            task_result = judge.task(evidence, task.rules, previous, tracker.values())
            notification_result = judge.notification(evidence, task.rules, tracker.values(), previous)
            for proposal in notification_result.new_opportunities:
                rule = next((r for r in task.rules if r.id == proposal.rule_id), None)
                event = next((e for e in pending_events if e.id == proposal.trigger_event_id), None)
                if rule is None or event is None or proposal.trigger_event_id not in rule.trigger_event_ids:
                    raise ValueError("judge proposed unknown rule or trigger event")
                tracker.create(rule, proposal.trigger_event_id, event.at.isoformat())
            for settlement in notification_result.settled:
                tracker.settle(settlement.opportunity_id, settlement.status, settlement.score, timestamp, settlement.reason, settlement.evidence_refs)
            for item in tracker.active():
                if datetime.fromisoformat(timestamp) > datetime.fromisoformat(item["notification_deadline"]):
                    tracker.settle(item["opportunity_id"], "EXPIRED", 0, timestamp, "Notification deadline passed.", ["tick:clock"])
                else:
                    item["status"] = "PENDING"
            related = [o for o in tracker.values() if o["created_at"] <= timestamp and (o["resolved_at"] == timestamp or o["status"] in ("OPEN", "PENDING"))]
            eligible_quiet = not had_active_obligation and not notification_result.new_opportunities
            false_count = len(notification_result.false_notifications)
            quiet_score = (-1 if false_count else 1) if eligible_quiet else None
            turn = {**evidence, "related_opportunities": [o["opportunity_id"] for o in related], "judge_result": {"task": task_result.model_dump(), "notification": notification_result.model_dump(), "not_required_score": quiet_score}}
            turns.append(turn)
            _json(run_dir / f"turns/turn_{turn_id:04d}.json", turn)
            pending_events.clear()
        if task.manifest.judge.get("completion_policy", "expire") == "expire":
            end = (task.manifest.initial_timestamp.timestamp() + task.manifest.duration_seconds)
            from datetime import timezone
            end_time = datetime.fromtimestamp(end, timezone.utc).isoformat()
            for item in tracker.active():
                tracker.settle(item["opportunity_id"], "EXPIRED", 0, end_time, "Task ended with outstanding notification obligation.", ["task:termination"])
        metrics = aggregate(turns, tracker.values())
        _json(run_dir / "ticks.json", ticks)
        _json(run_dir / "world_history.json", world.history())
        _json(run_dir / "evidence_hashes.json", hashes)
        _json(run_dir / "report.json", metrics)
        (run_dir / "report.txt").write_text(f"Task: {task.manifest.task_id}\nR_task: {metrics['R_task']}\nR_notify: {metrics['R_notify'] if metrics['R_notify'] is not None else 'N/A'}\nTurns: {metrics['total_agent_turns']}\nTask turn scores: {metrics['task_turn_scores']}\nRequired opportunities: {metrics['required_opportunity_count']} (mean: {metrics['R_required'] if metrics['R_required'] is not None else 'N/A'})\nEligible quiet turns: {metrics['eligible_not_required_turn_count']} (mean: {metrics['R_not_required'] if metrics['R_not_required'] is not None else 'N/A'})\nMissed notifications: {metrics['missed_notification_count']}\nFalse notifications: {metrics['false_notification_count']}\nOutstanding opportunities: {len(metrics['outstanding_opportunities'])}\nCancelled opportunities: {len(metrics['cancelled_opportunities'])}\nConstraint violations: {len(metrics['constraint_violations'])}\nJudge details: report.json and turns/\n", encoding="utf-8")
        return metrics
    finally:
        world.close()
        if hasattr(agent, "shutdown"):
            agent.shutdown()

