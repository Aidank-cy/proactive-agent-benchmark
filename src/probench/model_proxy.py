"""Narrow egress proxy: OpenClaw can reach only the configured model API."""
from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx


base = os.environ["AGENT_API_BASE_URL"].rstrip("/")
key = os.environ["AGENT_API_KEY"]
if urlparse(base).scheme not in ("http", "https"):
    raise ValueError("AGENT_API_BASE_URL must use HTTP(S)")


class Handler(BaseHTTPRequestHandler):
    def _forward(self):
        # 只转发模型列表与推理接口，OpenClaw 无法借此访问任意外部路径。
        if self.path not in ("/v1/chat/completions", "/v1/models") or self.command not in ("POST", "GET"):
            self.send_error(404)
            return
        target = base + self.path.removeprefix("/v1")
        content = self.rfile.read(int(self.headers.get("Content-Length", "0"))) if self.command == "POST" else None
        # 真正的 Agent API 密钥留在代理容器，发给上游时才注入。
        try:
            response = httpx.request(self.command, target, content=content, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": self.headers.get("Accept", "application/json")}, timeout=180)
        except httpx.HTTPError as exc:
            self.send_error(502, str(exc))
            return
        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    do_POST = _forward
    do_GET = _forward


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
