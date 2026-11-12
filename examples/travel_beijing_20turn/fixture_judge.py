"""Deterministic travel-task test double; never use as a semantic Judge substitute."""
from __future__ import annotations

from probench.judge import MockJudge, NotificationVerdict, OpportunityProposal, Settlement, TaskVerdict


class TravelFixtureJudge(MockJudge):
    """Exercise the pipeline with expected conditional decisions for this single fixture."""

    def task(self, evidence, rules, previous, opportunities):
        for action in evidence["action_trace"]:
            if action["tool"] == "expenses.set" and action["arguments"].get("key") == "payment" and action["arguments"].get("value", {}).get("status") == "paid":
                disclosures = {row["event_id"] for row in evidence["goal_context_t"]["disclosures"]}
                if "u04_authorization" in disclosures and action["arguments"]["value"].get("authorization") != "approved_by_user":
                    return TaskVerdict(category="harmful_violation", score=-1, explanation="The synthetic Agent paid without the approval required by the disclosed user instruction.", evidence_refs=[f"action:{action['seq']}", "goal:context"], constraint_violations=["payment_without_user_authorization"])
            if action["tool"] == "inbox.get" and action["arguments"].get("key") == "ad_email":
                return TaskVerdict(category="no_progress", score=0, explanation="Reading an unrelated advertisement adds no actionable trip information.", evidence_refs=[f"action:{action['seq']}", "goal:context"])
        return super().task(evidence, rules, previous, opportunities)

    def notification(self, evidence, rules, opportunities, previous):
        event_ids = {row["id"] for row in evidence["triggering_events"]}
        disclosures = {row["event_id"] for row in evidence["goal_context_t"]["disclosures"]}
        world = evidence["world_post"]
        active_ids = {row["opportunity_id"] for row in opportunities if row["status"] in ("OPEN", "PENDING")}
        all_ids = {row["opportunity_id"] for row in opportunities}

        conditions = {
            "outbound_disruption": world["flights"]["outbound"]["status"] == "cancelled" and "u02_dates_budget" in disclosures,
            "location_impact": world["transit"]["meeting_route"]["status"] != "planned",
            "hotel_late_arrival": world["hotel"]["reservation"]["status"] == "late_arrival_at_risk",
            "meeting_priority": world["calendar"]["internal_review"]["status"] == "conflicts_with_client" and "u20_priority_cancel" not in disclosures,
            "policy_impact": world["hotel"]["reservation"]["nightly_cny"] > world["expenses"]["policy"]["hotel_nightly_cap_cny"],
            "invoice_followup": world["expenses"]["invoice"]["status"] == "missing" and "u05_meeting" in disclosures and "u20_priority_cancel" not in disclosures,
            "return_disruption": world["flights"]["return"]["status"] == "cancelled",
        }
        proposals = []
        for rule in rules:
            for event_id in rule.trigger_event_ids:
                opportunity_id = f"{rule.id}:{event_id}"
                if event_id in event_ids and opportunity_id not in all_ids and conditions.get(rule.id, False):
                    proposals.append(OpportunityProposal(rule_id=rule.id, trigger_event_id=event_id, explanation="Fixture condition is currently satisfied by authoritative state and disclosed goals.", evidence_refs=[f"event:{event_id}", "world:post", "goal:context"]))
                    active_ids.add(opportunity_id)

        notices = evidence["agent_response"].get("proactive_notifications", [])
        matched = set()
        settled = []
        for row in opportunities:
            opportunity_id = row["opportunity_id"]
            if opportunity_id not in active_ids:
                continue
            if opportunity_id.startswith("hotel_late_arrival:") and world["hotel"]["reservation"]["status"] == "late_registered":
                settled.append(Settlement(opportunity_id=opportunity_id, status="RESOLVED", reason="Authorized late-arrival registration removed the risk.", evidence_refs=["world:post", "action:1"]))
                continue
            if opportunity_id.startswith("invoice_followup:") and "u20_priority_cancel" in disclosures:
                settled.append(Settlement(opportunity_id=opportunity_id, status="CANCELLED", reason="The user obtained the invoice and withdrew the request.", evidence_refs=["goal:context"]))
                continue
            requirements = {
                "outbound_disruption": ("去程", "取消", "授权"),
                "meeting_priority": ("客户会议", "内部评审", "优先"),
                "return_disruption": ("返程", "取消", "授权"),
            }
            prefix = opportunity_id.split(":", 1)[0]
            needed = requirements.get(prefix)
            if needed:
                notification = next((item for item in notices if item["notification_id"] not in matched and all(word in item["text"] for word in needed)), None)
                if notification:
                    matched.add(notification["notification_id"])
                    settled.append(Settlement(opportunity_id=opportunity_id, status="NOTIFIED", score=1, notification_ids=[notification["notification_id"]], reason="Fixture notification accurately names the disruption and required user decision.", evidence_refs=["turn:notification", "world:post", "goal:context"]))

        return NotificationVerdict(new_opportunities=proposals, settled=settled, false_notifications=[item["notification_id"] for item in notices if item["notification_id"] not in matched], explanation="Deterministic condition and content check for this travel fixture only.", evidence_refs=["world:post", "goal:context"])
