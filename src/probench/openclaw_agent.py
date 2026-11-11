"""OpenClaw Gateway adapter. OpenClaw owns planning, inference, tools, and session memory."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


class OpenClawAgent:
    external_tools = True

    def __init__(self, timeout: float | None = None):
        self.base_url = os.environ.get("OPENCLAW_GATEWAY_URL", "http://openclaw:18789").rstrip("/")
        self.token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        self.model = os.environ.get("AGENT_MODEL")
        self.provider_url = os.environ.get("AGENT_API_BASE_URL")
        self.timeout = timeout or float(os.environ.get("BENCH_AGENT_TIMEOUT", "180"))
        if not all((self.token, self.model, self.provider_url)):
            raise ValueError("OPENCLAW_GATEWAY_TOKEN, AGENT_API_BASE_URL, and AGENT_MODEL are required")
        self.session_key = f"bench-{uuid.uuid4().hex}"
        self.run_dir: Path | None = None
        self.turn_id = 0
        self.model_calls = 0

    def start_run(self, task, run_dir: Path):
        expected_name = os.environ.get("BENCH_RUN_NAME")
        if expected_name and run_dir.name != expected_name:
            raise ValueError(f"output directory name must match BENCH_RUN_NAME={expected_name}")
        plan_path = os.environ.get("BENCH_PLAN_PATH")
        if plan_path:
            from .service_plan import load_plan, plan_digest, verify_plan
            verify_plan(task, Path(plan_path))
            self.plan_digest = plan_digest(load_plan(Path(plan_path)))
        else:
            self.plan_digest = None
        self.task_id = task.manifest.task_id
        self.run_dir = run_dir
        self.metadata = {"runtime": "openclaw", "provider": "bench", "model": self.model, "api_base_url": self.provider_url, "temperature": os.environ.get("AGENT_TEMPERATURE", "0"), "context_window": os.environ.get("AGENT_CONTEXT_WINDOW", "128000"), "session_key": self.session_key, "gateway_url": self.base_url}
        (run_dir / "agent_config.json").write_text(json.dumps(self.metadata, indent=2), encoding="utf-8")
        deadline = time.monotonic() + 90
        while True:
            try:
                ready = httpx.get(self.base_url + "/v1/models", headers={"Authorization": f"Bearer {self.token}"}, timeout=3)
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("OpenClaw Gateway did not become ready within 90 seconds")
            time.sleep(2)

    def start_turn(self, turn_id: int, timestamp: str, permissions: dict[str, list[str]]):
        assert self.run_dir is not None
        self.turn_id = turn_id
        control = {"turn_id": turn_id, "timestamp": timestamp, "permissions": permissions, "task_id": self.task_id, "plan_digest": self.plan_digest}
        (self.run_dir / "world_control.json").write_text(json.dumps(control), encoding="utf-8")

    def run_turn(self, observation: dict[str, Any], tools, history: list[dict[str, Any]]) -> dict[str, Any]:
        if self.run_dir is None:
            raise RuntimeError("OpenClawAgent.start_run was not called")
        message = f"Task package: {self.task_id}. Benchmark Turn {self.turn_id}. Simulated time: {observation['timestamp']}. New observations: {json.dumps(observation['messages'], ensure_ascii=False)}. Use only benchmark world tools. Send unsolicited updates only with the notify_user tool. Your ordinary final answer is a reply to this invocation, not a proactive notification."
        headers = {"Authorization": f"Bearer {self.token}", "x-openclaw-model": f"bench/{self.model}"}
        payload = {"model": "openclaw/default", "user": self.session_key, "stream": False, "messages": [{"role": "user", "content": message}]}
        try:
            response = httpx.post(self.base_url + "/v1/chat/completions", headers=headers, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"OpenClaw Gateway turn {self.turn_id} failed: {exc}") from exc
        self.model_calls += 1
        (self.run_dir / f"openclaw_response_{self.turn_id:04d}.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        content = body["choices"][0]["message"].get("content") or ""
        return {"text": content, "proactive_notifications": []}

    def shutdown(self):
        # Revoke the MCP tool service's active-turn marker; Compose owns Gateway lifetime.
        if self.run_dir is not None:
            (self.run_dir / "world_control.json").unlink(missing_ok=True)
