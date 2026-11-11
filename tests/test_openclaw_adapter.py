import json

import pytest

from probench.openclaw_agent import OpenClawAgent


class Response:
    status_code = 200
    def raise_for_status(self):
        pass
    def json(self):
        return {"choices": [{"message": {"content": "done"}}]}


def test_gateway_adapter_reuses_session_and_separates_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://gateway:18789")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-secret")
    monkeypatch.setenv("AGENT_API_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("AGENT_API_KEY", "agent-secret")
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("BENCH_JUDGE_API_KEY", "judge-secret")
    requests = []
    monkeypatch.setattr("probench.openclaw_agent.httpx.get", lambda *a, **k: Response())
    def post(url, *, headers, json, timeout):
        requests.append((url, headers, json))
        return Response()
    monkeypatch.setattr("probench.openclaw_agent.httpx.post", post)
    agent = OpenClawAgent()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    class Task:
        class Manifest:
            task_id = "test_task"
        manifest = Manifest()
    agent.start_run(Task(), run_dir)
    for turn in (1, 2):
        agent.start_turn(turn, "2026-01-01T00:00:00+00:00", {"kv": ["get"]})
        result = agent.run_turn({"timestamp": "2026-01-01T00:00:00+00:00", "messages": [{"event_id": f"wake_{turn}", "kind": "scheduled_wakeup", "body": None}]}, None, [])
        assert result == {"text": "done", "proactive_notifications": []}
    assert requests[0][2]["user"] == requests[1][2]["user"]
    assert requests[0][1]["x-openclaw-model"] == "bench/test-model"
    assert "judge-secret" not in json.dumps(requests)
    assert "agent-secret" not in (run_dir / "agent_config.json").read_text()
    assert json.loads((run_dir / "world_control.json").read_text())["turn_id"] == 2
    agent.shutdown()
    assert not (run_dir / "world_control.json").exists()


def test_gateway_adapter_requires_independent_agent_config(monkeypatch):
    for name in ("OPENCLAW_GATEWAY_TOKEN", "AGENT_API_BASE_URL", "AGENT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="AGENT_API_BASE_URL"):
        OpenClawAgent()
