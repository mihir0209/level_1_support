"""CSAT-driven Priority SLA Escalation helpers."""

from __future__ import annotations

import structlog
from core.crm.customer_history import get_customer_history

log = structlog.get_logger(__name__)


async def is_customer_high_churn_risk(customer_id: str) -> bool:
    """
    Evaluates whether a customer is a high churn risk based on historic CSAT
    ratings (< 3.0) or prior escalated outcomes.
    """
    if not customer_id:
        return False

    try:
        history = await get_customer_history(customer_id)
        if not history:
            return False

        valid_scores = [h.get("satisfaction") for h in history if h.get("satisfaction") is not None]
        avg_csat = sum(valid_scores) / len(valid_scores) if valid_scores else None
        has_prior_escalations = any(h.get("outcome") == "escalated" for h in history)

        if (avg_csat is not None and avg_csat < 3.0) or has_prior_escalations:
            log.info("customer_flagged_as_high_churn_risk", customer_id=customer_id, avg_csat=avg_csat, has_prior_escalations=has_prior_escalations)
            return True
    except Exception as exc:
        log.warning("failed_to_evaluate_churn_risk", customer_id=customer_id, error=str(exc))

    return False
