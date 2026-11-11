"""Offline OpenAI-compatible test API; it is not a benchmark agent or judge."""
from __future__ import annotations

import json
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .judge import MockJudge
from .schema import NotificationRule


def _text(message):
    content = message.get("content") or ""
    if isinstance(content, list):
        return " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    return str(content)


def _agent_reply(body):
    messages = body.get("messages", [])
    users = [(i, _text(x)) for i, x in enumerate(messages) if x.get("role") == "user"]
    if not users:
        return {"role": "assistant", "content": "No turn found."}, "stop"
    idx, user = users[-1]
    match = re.search(r"Benchmark Turn (\d+)", user)
    turn = int(match.group(1)) if match else 0
    has_tool_result = any(x.get("role") == "tool" for x in messages[idx + 1:])
    if has_tool_result or turn == 4:
        return {"role": "assistant", "content": f"Synthetic Turn {turn} complete."}, "stop"
    calls = {1: ("world__world_set", {"key": "item", "value": 1}), 2: ("world__world_get", {"key": "signal"}), 3: ("world__notify_user", {"text": "The signal changed.", "opportunity_id": "material_signal:change"})}
    if turn not in calls:
        return {"role": "assistant", "content": "Routine check complete."}, "stop"
    name, args = calls[turn]
    available = {x["function"]["name"] for x in body.get("tools", []) if x.get("type") == "function"}
    if name not in available:
        return {"role": "assistant", "content": f"Required tool unavailable: {name}"}, "stop"
    return {"role": "assistant", "content": None, "tool_calls": [{"id": f"call-{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "tool_calls"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = {"object": "list", "data": [{"id": "fixture-model", "object": "model"}, {"id": "fixture-judge", "object": "model"}]}
        self._send(json.dumps(data).encode())

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        print(f"MOCK_MODEL_REQUEST model={body.get('model')} stream={bool(body.get('stream'))}", flush=True)
        if body.get("model") == "fixture-judge":
            request = json.loads(_text(body["messages"][-1]))
            judge = MockJudge()
            if "Judge task execution" in _text(body["messages"][0]):
                verdict = judge.task(request["evidence"], [NotificationRule.model_validate(x) for x in request["hidden_rules"]], request["previous_evidence"], request["opportunities"])
            else:
                verdict = judge.notification(request["evidence"], [NotificationRule.model_validate(x) for x in request["hidden_rules"]], request["opportunities"], request.get("previous_evidence", []))
            message, finish = {"role": "assistant", "content": verdict.model_dump_json()}, "stop"
        else:
            message, finish = _agent_reply(body)
        response = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()), "model": body.get("model"), "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        if body.get("stream"):
            delta = {"role": "assistant"}
            if message.get("tool_calls"):
                delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
            else:
                delta["content"] = message["content"]
            chunks = [dict(response, object="chat.completion.chunk", choices=[{"index": 0, "delta": delta, "finish_reason": None}]), dict(response, object="chat.completion.chunk", choices=[{"index": 0, "delta": {}, "finish_reason": finish}])]
            data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            self._send(data, "text/event-stream")
        else:
            self._send(json.dumps(response).encode())

    def _send(self, data: bytes, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
