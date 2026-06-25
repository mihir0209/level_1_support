"""
Feedback Engine — closed-loop improvement for L1 Support.

Processes failed/flagged sessions from the resolution review queue and updates
the knowledge graph with resolution quality signals.

Responsibilities:
  1. Consume sessions from resolution_review_queue (low-confidence or
     failed resolutions flagged during conversation)
  2. Update Resolution.success_rate in the KG after each resolved session
  3. Identify low-confidence intent classifications → feed into intent_label_queue
  4. Surface resolution failure patterns to operators via session stats

The engine is lightweight — triggered on session close/escalate from
node_close_session and node_escalate in orchestration/graph.py.
"""

from __future__ import annotations

from typing import Any

import structlog

from config import settings
from core.knowledge.retriever import update_resolution_success_rate

log = structlog.get_logger(__name__)

# Confidence thresholds
LOW_CONFIDENCE_THRESHOLD = settings.low_confidence_threshold
CSAT_DISSATISFIED_THRESHOLD = settings.csat_dissatisfied_threshold


def _get_db_conn():
    """Get connection from global sync pool using ConnectionProxy."""
    from core.db.pool import get_sync_pool, ConnectionProxy
    return ConnectionProxy(get_sync_pool())


def _queue_intent_review(brand_id: str, intent: str, confidence: float) -> None:
    """Add a low-confidence intent classification to the review queue."""
    conn = None
    try:
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intent_label_queue (brand_id, raw_message, suggested_label)
                    VALUES (%s, %s, %s)
                """, (brand_id, intent, intent))
        log.debug("intent_queued_for_review", brand_id=brand_id, intent=intent, confidence=confidence)
    except Exception as e:
        log.warning("intent_queue_failed", brand_id=brand_id, error=str(e))
    finally:
        if conn:
            conn.close()


def _queue_resolution_review(
    brand_id: str,
    issue_type: str,
    resolution_tried: str | None,
    failure_reason: str,
    conversation: dict | None = None,
) -> None:
    """Add a failed resolution to the review queue for human analysis."""
    conn = None
    try:
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO resolution_review_queue
                        (brand_id, issue_type, resolution_tried, failure_reason, conversation)
                    VALUES (%s, %s, %s, %s, %s)
                """, (brand_id, issue_type, resolution_tried, failure_reason, conversation))
        log.debug("resolution_queued_for_review", brand_id=brand_id, issue_type=issue_type)
    except Exception as e:
        log.warning("resolution_queue_failed", brand_id=brand_id, error=str(e))
    finally:
        if conn:
            conn.close()


def on_session_close(
    session_id: str,
    brand_id: str,
    resolution_id: str | None,
    satisfaction: float | None,
    intent: str,
    confidence: float,
    conversation: dict | None = None,
) -> None:
    """
    Called when a session closes (resolved or escalated).
    Updates the KG success rate and queues low-confidence items for review.
    """
    outcome = "resolved" if resolution_id else "escalated"

    # 1. Update KG success rate for the resolution that was used
    if resolution_id:
        _update_resolution_stats(resolution_id, resolved=True, satisfaction=satisfaction)

    # 2. Queue low-confidence intent classifications for human review
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        _queue_intent_review(brand_id, intent, confidence)

    # 3. Queue failed resolutions for review
    if outcome == "escalated" and resolution_id:
        _queue_resolution_review(
            brand_id=brand_id,
            issue_type=intent,
            resolution_tried=resolution_id,
            failure_reason=f"confidence={confidence:.2f}, satisfaction={satisfaction}",
            conversation=conversation,
        )
    elif outcome == "resolved" and satisfaction is not None:
        if satisfaction < CSAT_DISSATISFIED_THRESHOLD:
            _queue_resolution_review(
                brand_id=brand_id,
                issue_type=intent,
                resolution_tried=resolution_id,
                failure_reason=f"low_csat={satisfaction:.2f}",
                conversation=conversation,
            )

    log.info(
        "feedback_processed",
        session_id=session_id,
        outcome=outcome,
        resolution_id=resolution_id,
        satisfaction=satisfaction,
        confidence=confidence,
    )


def on_session_escalate(
    session_id: str,
    brand_id: str,
    resolution_id: str | None,
    intent: str,
    reason: str,
    conversation: dict | None = None,
) -> None:
    """
    Called when a session escalates to a human agent.
    Always queues for review since escalation means L1 failed.
    """
    _queue_resolution_review(
        brand_id=brand_id,
        issue_type=intent,
        resolution_tried=resolution_id,
        failure_reason=reason,
        conversation=conversation,
    )
    log.info(
        "escalation_queued_for_review",
        session_id=session_id,
        brand_id=brand_id,
        reason=reason[:100],
    )


def _update_resolution_stats(
    resolution_id: str,
    resolved: bool,
    satisfaction: float | None,
) -> None:
    """Update Resolution.success_rate in the KG via the retriever's feedback function."""
    try:
        update_resolution_success_rate(
            resolution_id=resolution_id,
            resolved=resolved,
            satisfaction=satisfaction or 0.0,
        )
    except Exception as e:
        log.warning("feedback_update_failed", resolution_id=resolution_id, error=str(e))


def get_review_queue_stats(brand_id: str | None = None) -> dict[str, Any]:
    """
    Return counts of items in the review queues.
    Used by the admin dashboard to surface systemic issues.
    """
    try:
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                where_clause = ""
                params: list = []
                if brand_id:
                    where_clause = "WHERE brand_id = %s AND reviewed = false"
                    params = [brand_id]
                else:
                    where_clause = "WHERE reviewed = false"

                cur.execute(f"""
                    SELECT COUNT(*) FROM intent_label_queue {where_clause}
                """, params)
                intent_row = cur.fetchone()
                intent_pending = intent_row[0] if intent_row else 0

                cur.execute(f"""
                    SELECT COUNT(*) FROM resolution_review_queue {where_clause}
                """, params)
                res_row = cur.fetchone()
                resolution_pending = res_row[0] if res_row else 0

                # Top unresolved intents
                cur.execute("""
                    SELECT brand_id, suggested_label, COUNT(*) as cnt
                    FROM intent_label_queue
                    WHERE reviewed = false
                    GROUP BY brand_id, suggested_label
                    ORDER BY cnt DESC
                    LIMIT 5
                """)
                top_intents = cur.fetchall() or []

                # Top failure reasons
                cur.execute("""
                    SELECT issue_type, failure_reason, COUNT(*) as cnt
                    FROM resolution_review_queue
                    WHERE reviewed = false
                    GROUP BY issue_type, failure_reason
                    ORDER BY cnt DESC
                    LIMIT 5
                """)
                top_failures = cur.fetchall() or []

        conn.close()

        return {
            "intent_labels_pending": intent_pending,
            "resolution_reviews_pending": resolution_pending,
            "top_intents_needing_review": [
                {"brand": b, "intent": i, "count": int(c)} for b, i, c in top_intents
            ],
            "top_failure_reasons": [
                {"issue_type": it, "reason": str(r)[:80], "count": int(c)} for it, r, c in top_failures
            ],
        }
    except Exception as e:
        log.warning("review_queue_stats_failed", error=str(e))
        return {"error": str(e)}


def approve_intent_label(queue_id: str, approved_label: str) -> bool:
    """Mark an intent label as reviewed and approved."""
    try:
        import uuid
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE intent_label_queue
                    SET reviewed = true, approved_label = %s
                    WHERE id = %s
                """, (approved_label, uuid.UUID(queue_id)))
        conn.close()
        log.info("intent_label_approved", queue_id=queue_id, label=approved_label)
        return True
    except Exception as e:
        log.warning("intent_approval_failed", queue_id=queue_id, error=str(e))
        return False


def dismiss_resolution_review(queue_id: str) -> bool:
    """Mark a resolution review as dismissed (no action needed)."""
    try:
        import uuid
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE resolution_review_queue
                    SET reviewed = true
                    WHERE id = %s
                """, (uuid.UUID(queue_id),))
        conn.close()
        log.info("resolution_review_dismissed", queue_id=queue_id)
        return True
    except Exception as e:
        log.warning("resolution_dismiss_failed", queue_id=queue_id, error=str(e))
        return False
