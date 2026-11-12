"""Apply one Notification Judge verdict to the authoritative run state."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .judge import NotificationAssessment, NotificationVerdict
from .run_store import RunStore
from .schema import Event, TaskPackage
from .scoring import OpportunityTracker


def apply_notification_verdict(
    task: TaskPackage,
    tracker: OpportunityTracker,
    store: RunStore,
    verdict: NotificationVerdict,
    events: list[Event],
    timestamp: str,
    tick_id: int,
    turn_id: int | None,
    notifications: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Validate Judge decisions against the current Tick and real MCP deliveries."""
    notifications = {row["notification_id"]: row for row in (notifications or [])}
    false_ids = list(verdict.false_notifications)
    if len(false_ids) != len(set(false_ids)) or set(false_ids) - notifications.keys():
        raise ValueError("Judge cited duplicate or non-current false notification ID")
    event_by_id = {event.id: event for event in events}
    rules = {rule.id: rule for rule in task.rules}

    def resolve_target(decision):
        if decision.opportunity_id:
            oid = decision.opportunity_id
        elif decision.rule_id in rules and decision.dedupe_key:
            oid = tracker.proposal_id(rules[decision.rule_id], "", decision.dedupe_key)
        else:
            raise ValueError("Judge cited an incomplete opportunity reference")
        if oid not in tracker.items:
            raise ValueError(f"Judge cited unknown opportunity: {oid}")
        # Store the validated canonical ID; raw LLM output is preserved separately.
        decision.opportunity_id = oid
        decision.rule_id = None
        decision.dedupe_key = None
        return oid

    created_ids = []
    for proposal in verdict.new_opportunities:
        rule, event = rules.get(proposal.rule_id), event_by_id.get(proposal.trigger_event_id)
        if rule is None or event is None:
            raise ValueError("Judge proposed unknown rule or trigger event")
        origin_tick_id = store.event_tick_id(event.id)
        oid = tracker.proposal_id(rule, event.id, proposal.dedupe_key)
        was_new = oid not in tracker.items
        item = tracker.create(rule, event.id, event.at.isoformat(), proposal.explanation,
                              proposal.evidence_refs, tick_id=origin_tick_id, turn_id=turn_id,
                              dedupe_key=proposal.dedupe_key)
        if was_new:
            created_ids.append(oid)
        if proposal.actionable_now:
            if turn_id is None:
                raise ValueError("Silent Tick cannot contain an actionable Agent Turn")
            if item["status"] in ("OPEN", "PENDING") and datetime.fromisoformat(timestamp) <= datetime.fromisoformat(item["notification_deadline"]):
                tracker.mark_actionable(oid, timestamp, turn_id, proposal.evidence_refs,
                                        proposal.explanation, tick_id=tick_id)
    if verdict.actionable and turn_id is None:
        raise ValueError("Silent Tick cannot attribute an actionable Agent Turn")
    for assessment in verdict.actionable:
        if assessment.opportunity_id not in tracker.items:
            raise ValueError(f"Judge cited unknown actionable opportunity: {assessment.opportunity_id}")
        item = tracker.items[assessment.opportunity_id]
        if item["status"] in ("OPEN", "PENDING") and datetime.fromisoformat(timestamp) <= datetime.fromisoformat(item["notification_deadline"]):
            tracker.mark_actionable(item["opportunity_id"], timestamp, turn_id,
                                    assessment.evidence_refs, assessment.explanation, tick_id=tick_id)

    attempts = list(verdict.assessments)
    closures = []
    seen_closures = set()
    for settlement in verdict.settled:
        oid = resolve_target(settlement)
        if oid in seen_closures:
            raise ValueError(f"Judge settled opportunity twice: {oid}")
        seen_closures.add(oid)
        if settlement.status == "NOTIFIED":
            quality = {1.0: "complete", 0.5: "partial", -1.0: "harmful"}.get(settlement.score)
            if quality is None:
                raise ValueError("invalid NOTIFIED score; use a notification assessment")
            attempts.append(NotificationAssessment(opportunity_id=oid,
                notification_ids=settlement.notification_ids, quality=quality,
                explanation=settlement.reason, evidence_refs=settlement.evidence_refs))
        else:
            closures.append(settlement)
    # A legacy NOTIFIED (+0.5) verdict is an attempt, not a final settlement.
    # The raw API output remains in judgments.raw_output; the validated verdict
    # records assessments and only genuine closure requests under `settled`.
    verdict.assessments = attempts
    verdict.settled = closures

    matched = set()
    harmful_matched = set()
    late = set()
    for assessment in attempts:
        oid = resolve_target(assessment)
        if oid not in tracker.items or tracker.items[oid]["status"] not in ("OPEN", "PENDING"):
            raise ValueError(f"Judge cited unknown or settled opportunity: {oid}")
        if turn_id is None:
            raise ValueError("notification attempt cannot occur on a Silent Tick")
        if set(assessment.notification_ids) - notifications.keys():
            raise ValueError(f"Judge matched notification outside the current Turn: {oid}")
        for notification_id in assessment.notification_ids:
            store.validate_delivery(notification_id, turn_id)
        deadline = datetime.fromisoformat(tracker.items[oid]["notification_deadline"])
        if any(datetime.fromisoformat(notifications[nid]["timestamp"]) > deadline for nid in assessment.notification_ids):
            late.update(assessment.notification_ids)
            continue
        tracker.record_attempt(oid, assessment.quality, assessment.notification_ids, timestamp,
                               assessment.explanation, assessment.evidence_refs, tick_id=tick_id, turn_id=turn_id)
        matched.update(assessment.notification_ids)
        if assessment.quality == "harmful":
            harmful_matched.update(assessment.notification_ids)
        if assessment.quality in ("complete", "harmful"):
            tracker.settle(oid, "NOTIFIED", 1 if assessment.quality == "complete" else -1,
                           timestamp, assessment.explanation, assessment.evidence_refs,
                           tick_id=tick_id, turn_id=turn_id)

    for settlement in closures:
        oid = settlement.opportunity_id
        if oid not in tracker.items or tracker.items[oid]["status"] not in ("OPEN", "PENDING"):
            raise ValueError(f"Judge cited unknown or settled opportunity: {oid}")
        if settlement.status in ("RESOLVED", "CANCELLED"):
            tracker.settle(oid, settlement.status, None, timestamp, settlement.reason,
                           settlement.evidence_refs, tick_id=tick_id, turn_id=turn_id)
        elif settlement.status == "EXPIRED":
            if datetime.fromisoformat(timestamp) <= datetime.fromisoformat(tracker.items[oid]["notification_deadline"]):
                raise ValueError("Judge expired an opportunity before its deadline")
            tracker.expire(oid, timestamp, tick_id=tick_id, turn_id=turn_id)
        else:
            raise ValueError(f"unsupported settlement status: {settlement.status}")

    false_ids.extend(nid for nid in late if nid not in matched)
    false_ids = [nid for nid in false_ids if nid not in harmful_matched]
    if set(false_ids) & matched:
        raise ValueError("Judge marked a matched notification as false")
    if set(notifications) - matched - set(false_ids):
        raise ValueError("Judge left current notifications unclassified")
    verdict.false_notifications = list(dict.fromkeys(false_ids))
    for item in list(tracker.active()):
        if datetime.fromisoformat(timestamp) > datetime.fromisoformat(item["notification_deadline"]):
            tracker.expire(item["opportunity_id"], timestamp, tick_id=tick_id, turn_id=turn_id)
        elif turn_id is not None:
            tracker.mark_pending(item["opportunity_id"], timestamp, tick_id=tick_id, turn_id=turn_id)
    return created_ids
