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
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    trigger_event_id: str
    explanation: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opportunity_id: str
    status: Literal["NOTIFIED", "RESOLVED", "EXPIRED", "CANCELLED"]
    score: Literal[-1.0, 0.0, 0.5, 1.0] | None = None
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_score(self):
        if (self.status in ("NOTIFIED", "EXPIRED")) != (self.score is not None):
            raise ValueError("settlement score/status mismatch")
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
    def __init__(self):
        self.base_url = os.environ.get("BENCH_JUDGE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get("BENCH_JUDGE_API_KEY")
        self.model = os.environ.get("BENCH_JUDGE_MODEL")
        if not self.key or not self.model:
            raise ValueError("BENCH_JUDGE_API_KEY and BENCH_JUDGE_MODEL are required")
        self.instructions = ""
        self.loader: JudgeEvidenceLoader | None = None

    def configure(self, task, run_dir, history):
        self.instructions = task.judge_instructions
        self.loader = JudgeEvidenceLoader(run_dir, task.judge_evidence_sources, history)

    def _payload(self, evidence, rules, previous, opportunities):
        data = self.loader.assemble(evidence, previous, opportunities) if self.loader else {
            "evidence": evidence, "previous_evidence": previous, "opportunities": opportunities,
        }
        data["task_instructions"] = self.instructions
        data["hidden_rules"] = [rule.model_dump() for rule in rules]
        return data

    @staticmethod
    def _check_refs(verdict, payload):
        evidence = payload["evidence"]
        allowed = {"world:pre", "world:post", "goal:context", "turn:response", "turn:notification"}
        allowed.update(f"event:{item['id']}" for item in evidence.get("triggering_events", []))
        allowed.update(f"action:{item['seq']}" for item in evidence.get("action_trace", []))
        allowed.update(f"opportunity:{item['opportunity_id']}" for item in payload.get("opportunities", []))
        allowed.update(f"history:{item['seq']}" for item in payload.get("world_history", []))
        allowed.update(f"previous_turn:{item['turn_id']}" for item in payload.get("previous_evidence", []))
        allowed.update(f"previous_notification:{item['turn_id']}" for item in payload.get("previous_notifications", []))
        groups = [verdict.evidence_refs]
        if isinstance(verdict, NotificationVerdict):
            groups.extend(item.evidence_refs for item in [*verdict.new_opportunities, *verdict.settled])
        for refs in groups:
            for ref in refs:
                if ref not in allowed:
                    raise ValueError(f"Judge cited unavailable evidence: {ref}")
        if isinstance(verdict, NotificationVerdict):
            for proposal in verdict.new_opportunities:
                if not any(not ref.startswith("event:") for ref in proposal.evidence_refs):
                    raise ValueError("notification opportunity needs condition evidence beyond an event ID")

    def _ask(self, kind: str, payload: dict[str, Any], model):
        schema = model.model_json_schema()
        prompt = RUBRIC + "\nJUDGE_REQUEST_KIND: " + kind + ". Return JSON matching this schema: " + json.dumps(schema)
        response = httpx.post(self.base_url + "/chat/completions", headers={"Authorization": f"Bearer {self.key}"}, json={"model": self.model, "temperature": 0, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, default=str)}], "response_format": {"type": "json_object"}}, timeout=90)
        response.raise_for_status()
        try:
            raw = response.json()["choices"][0]["message"]["content"]
            if not isinstance(raw, str):
                raise TypeError("content is not text")
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
        claimed = set()
        settled = []
        false = []
        for notification in notifications:
            oid = notification.get("opportunity_id")
            if oid in open_ids and oid not in claimed:
                claimed.add(oid)
                settled.append({"opportunity_id": oid, "status": "NOTIFIED", "score": 1.0, "reason": "Synthetic notification matched open opportunity.", "evidence_refs": ["turn:notification"]})
            else:
                false.append(json.dumps(notification, sort_keys=True))
        return NotificationVerdict(new_opportunities=proposals, settled=settled, false_notifications=false, explanation="Deterministic fixture notification check.", evidence_refs=["turn:response"])

