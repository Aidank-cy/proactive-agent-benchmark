"""独立 Judge 接口、结构化判词、全局 rubric 和 API 客户端。"""
from __future__ import annotations

import json
import os
from importlib.resources import files
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .judge_evidence import JudgeEvidenceLoader
from .schema import NotificationRule


class TaskVerdict(BaseModel):
    """一个完整 Agent Turn 的任务评分；类别与数值必须一一对应。"""

    model_config = ConfigDict(extra="forbid")
    category: Literal["goal_progress", "useful_information", "no_progress", "harmful_violation"]
    score: Literal[-1.0, 0.0, 0.5, 1.0]
    explanation: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    constraint_violations: list[str] = Field(default_factory=list)

    def model_post_init(self, __context):
        expected = {"goal_progress": 1.0, "useful_information": 0.5, "no_progress": 0.0, "harmful_violation": -1.0}
        if self.score != expected[self.category]:
            raise ValueError("category and score disagree")


class OpportunityProposal(BaseModel):
    """Judge 确认通知条件成立后，向跨 Turn 跟踪器提出新义务。"""

    model_config = ConfigDict(extra="forbid")
    rule_id: str
    trigger_event_id: str
    explanation: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class Settlement(BaseModel):
    """一项现有通知义务的最终结果，关闭后不得重复计分。"""

    model_config = ConfigDict(extra="forbid")
    opportunity_id: str
    status: Literal["NOTIFIED", "RESOLVED", "EXPIRED", "CANCELLED"]
    score: Literal[-1.0, 0.0, 0.5, 1.0] | None = None
    notification_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_score(self):
        if (self.status in ("NOTIFIED", "EXPIRED")) != (self.score is not None):
            raise ValueError("settlement score/status mismatch")
        if self.status == "NOTIFIED" and not self.notification_ids:
            raise ValueError("NOTIFIED settlement requires notification_ids")
        if self.status in ("RESOLVED", "CANCELLED") and self.notification_ids:
            raise ValueError("resolved or cancelled opportunity cannot claim notifications")
        if len(self.notification_ids) != len(set(self.notification_ids)):
            raise ValueError("duplicate notification ID in settlement")
        return self


class NotificationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_opportunities: list[OpportunityProposal] = Field(default_factory=list)
    settled: list[Settlement] = Field(default_factory=list)
    false_notifications: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class Judge(Protocol):
    def task(self, evidence: dict[str, Any], rules: list[NotificationRule], previous: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> TaskVerdict: ...
    def notification(self, evidence: dict[str, Any], rules: list[NotificationRule], opportunities: list[dict[str, Any]], previous: list[dict[str, Any]]) -> NotificationVerdict: ...


RUBRIC = files("probench").joinpath("global_judge_rubric.md").read_text(encoding="utf-8")


class OpenAICompatibleJudge:
    """通过独立 API 请求任务和通知判定，并校验结构及证据引用。"""

    def __init__(self):
        self.base_url = os.environ.get("BENCH_JUDGE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get("BENCH_JUDGE_API_KEY")
        self.model = os.environ.get("BENCH_JUDGE_MODEL")
        if not self.key or not self.model:
            raise ValueError("BENCH_JUDGE_API_KEY and BENCH_JUDGE_MODEL are required")
        self.instructions = ""
        self.loader: JudgeEvidenceLoader | None = None
        self.last_request: dict[str, Any] | None = None
        self.last_raw_output: str | None = None

    def configure(self, task, run_dir, history):
        self.instructions = task.judge_instructions
        self.loader = JudgeEvidenceLoader(run_dir, task.judge_evidence_sources, history)

    def _payload(self, evidence, rules, previous, opportunities):
        # 加载器筛选权威证据；任务说明和隐藏规则只进入 Judge 请求。
        data = self.loader.assemble(evidence, previous, opportunities) if self.loader else {
            "evidence": evidence, "previous_evidence": previous, "opportunities": opportunities,
        }
        data["task_instructions"] = self.instructions
        data["hidden_rules"] = [rule.model_dump() for rule in rules]
        return data

    @staticmethod
    def _check_refs(verdict, payload):
        """拒绝指向未提供证据的引用，防止判词凭空引用未来或不存在的动作。"""
        evidence = payload.get("evidence", {})
        allowed = {"world:pre", "world:post", "goal:context", "turn:response", "turn:notification"}
        allowed.update(f"event:{item.get('event_id', item.get('id'))}" for item in payload.get("events", evidence.get("triggering_events", [])))
        allowed.update(f"action:{item['seq']}" for item in payload.get("actions", evidence.get("action_trace", [])))
        relevant = payload.get("relevant_history", payload)
        allowed.update(f"opportunity:{item['opportunity_id']}" for item in relevant.get("opportunities", payload.get("opportunities", [])))
        allowed.update(f"history:{item['seq']}" for item in relevant.get("world_history", []))
        allowed.update(f"previous_turn:{item['turn_id']}" for item in relevant.get("previous_turns", payload.get("previous_evidence", [])))
        allowed.update(f"previous_notification:{item['turn_id']}" for item in relevant.get("previous_notifications", []))
        groups = [verdict.evidence_refs]
        if isinstance(verdict, NotificationVerdict):
            groups.extend(item.evidence_refs for item in [*verdict.new_opportunities, *verdict.settled])
        for refs in groups:
            for ref in refs:
                if ref not in allowed:
                    raise ValueError(f"Judge cited unavailable evidence: {ref}")
        if isinstance(verdict, NotificationVerdict):
            for proposal in verdict.new_opportunities:
                # 候选事件 ID 只是线索，不能单独证明通知必要性。
                if not any(not ref.startswith("event:") for ref in proposal.evidence_refs):
                    raise ValueError("notification opportunity needs condition evidence beyond an event ID")

    def _ask(self, kind: str, payload: dict[str, Any], model):
        """请求独立 Judge API，并按指定判词模型校验 JSON 和证据引用。

        `kind` 分开任务执行与通知判定；返回内容不能仅因是 JSON 就被
        接受，还必须符合分数字段约束、引用已提供的证据。
        """
        schema = model.model_json_schema()
        # 全局规则放在 system；任务说明与环境内容作为不可信数据放在 user。
        prompt = RUBRIC + "\nJUDGE_REQUEST_KIND: " + kind + ". Return JSON matching this schema: " + json.dumps(schema)
        request_body = {"model": self.model, "temperature": 0, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, default=str)}], "response_format": {"type": "json_object"}}
        self.last_request = request_body
        response = httpx.post(self.base_url + "/chat/completions", headers={"Authorization": f"Bearer {self.key}"}, json=request_body, timeout=90)
        response.raise_for_status()
        try:
            raw = response.json()["choices"][0]["message"]["content"]
            if not isinstance(raw, str):
                raise TypeError("content is not text")
            self.last_raw_output = raw
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed {kind} judge API response") from exc
        try:
            verdict = model.model_validate_json(raw)
        except ValidationError as exc:
            raise ValueError(f"malformed {kind} judge output: {exc}") from exc
        self._check_refs(verdict, payload)
        return verdict

    def task(self, evidence, rules, previous, opportunities):
        return self._ask("task execution", self._payload(evidence, rules, previous, opportunities), TaskVerdict)

    def notification(self, evidence, rules, opportunities, previous):
        return self._ask("notification", self._payload(evidence, rules, previous, opportunities), NotificationVerdict)


class MockJudge:
    """Deterministic infrastructure test double; not a quality measure for LLM scoring."""
    def task(self, evidence, rules, previous, opportunities):
        # 固定映射仅验证管线与分数聚合，不能替代真实 LLM 的语义判断。
        actions = evidence["action_trace"]
        violations = [a for a in actions if not a["success"] and "unavailable" in (a["error"] or "")]
        if violations:
            return TaskVerdict(category="harmful_violation", score=-1, explanation="Unauthorized tool attempt in synthetic fixture.", evidence_refs=[f"action:{a['seq']}" for a in violations], constraint_violations=[a["error"] for a in violations])
        writes = [a for a in actions if a["success"] and a["changes"]]
        if writes:
            return TaskVerdict(category="goal_progress", score=1, explanation="Authoritative state changed in the scripted fixture.", evidence_refs=[f"action:{a['seq']}" for a in writes])
        previous_reads = {(a["tool"], json.dumps(a["result"], sort_keys=True)) for p in previous for a in p.get("action_trace", []) if a["success"] and a.get("read_only", False)}
        reads = [a for a in actions if a["success"] and a.get("read_only", False) and (a["tool"], json.dumps(a["result"], sort_keys=True)) not in previous_reads and a["result"] is not None]
        if reads:
            return TaskVerdict(category="useful_information", score=0.5, explanation="New read result in the scripted fixture.", evidence_refs=[f"action:{a['seq']}" for a in reads])
        return TaskVerdict(category="no_progress", score=0, explanation="No new state or information in the scripted fixture.", evidence_refs=["turn:response"])

    def notification(self, evidence, rules, opportunities, previous):
        ids = {e["id"] for e in evidence["triggering_events"]}
        proposals = [OpportunityProposal(rule_id=r.id, trigger_event_id=eid, explanation="Configured synthetic trigger occurred.", evidence_refs=[f"event:{eid}", "world:post", "goal:context"]) for r in rules for eid in r.trigger_event_ids if eid in ids]
        notifications = evidence["agent_response"]["proactive_notifications"]
        open_ids = [o["opportunity_id"] for o in opportunities if o["status"] in ("OPEN", "PENDING")]
        open_ids.extend(f"{item.rule_id}:{item.trigger_event_id}" for item in proposals if f"{item.rule_id}:{item.trigger_event_id}" not in open_ids)
        claimed = set()
        settled = []
        false = []
        for notification in notifications:
            # 仅为合成夹具做确定性匹配；生产 Judge 使用当前世界和完整证据。
            signal = evidence["world_post"].get("kv", {}).get("signal")
            oid = next((item for item in open_ids if item not in claimed), None)
            if oid and signal is not None and notification["text"] == f"The signal {signal}.":
                claimed.add(oid)
                settled.append({"opportunity_id": oid, "status": "NOTIFIED", "score": 1.0, "notification_ids": [notification["notification_id"]], "reason": "Synthetic notification matched open opportunity.", "evidence_refs": ["turn:notification"]})
            else:
                false.append(notification["notification_id"])
        return NotificationVerdict(new_opportunities=proposals, settled=settled, false_notifications=false, explanation="Deterministic fixture notification check.", evidence_refs=["turn:response"])

