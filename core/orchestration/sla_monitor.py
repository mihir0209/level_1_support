"""Dynamic SLA Compliance Monitor."""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def check_sla_status(
    session_id: str,
    turn_count: int,
    escalation_risk: float
) -> str:
    """
    Evaluate session parameters to verify SLA compliance.
    Returns: 'ok', 'warning', or 'breached'.
    """
    # Thresholds:
    # Breached if turn_count > 8 or risk > 0.90
    if turn_count > 8 or escalation_risk > 0.90:
        status = "breached"
    # Warning if turn_count > 5 or risk > 0.75
    elif turn_count > 5 or escalation_risk > 0.75:
        status = "warning"
    else:
        status = "ok"

    log.info("sla_compliance_checked", session_id=session_id, turn_count=turn_count, escalation_risk=escalation_risk, status=status)
    return status
