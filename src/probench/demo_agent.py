"""Offline inspection Agent for pipeline smoke tests, not a benchmark model."""
from __future__ import annotations


class InspectAgent:
    """Choose one read-only query from the current observation's content."""

    def run_turn(self, observation, tools, history):
        message = " ".join(item["body"] for item in observation["messages"])
        if any(word in message for word in ("航班", "机票", "出差", "旅行部")):
            service = "flight"
        elif "酒店" in message:
            service = "hotel"
        elif any(word in message for word in ("日历", "会议")):
            service = "calendar"
        else:
            service = "email"
        tools.call(service, "list")
        return {"text": "已查看当前资料。", "proactive_notifications": []}
