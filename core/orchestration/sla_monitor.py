"""Dynamic SLA Compliance Monitor."""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


def get_grace_offsets(session_count: int, is_priority: bool) -> tuple[int, int]:
    """Calculate SLA warning and breach turn offsets based on Redis active session load."""
    if session_count < 10:
        return 0, 0
    elif session_count <= 25:
        if is_priority:
            return 0, 0
        else:
            return 1, 2
    else: # high load (> 25)
        if is_priority:
            return 1, 1
        else:
            return 3, 4


async def check_sla_status(
    session_id: str,
    turn_count: int,
    escalation_risk: float
) -> str:
    """
    Evaluate session parameters to verify SLA compliance.
    Returns: 'ok', 'warning', or 'breached'.
    """
    from core.memory.session import get_session, update_session
    from core.orchestration.csat_sla import is_customer_high_churn_risk

    # Default thresholds:
    warning_turn = 5
    breach_turn = 8
    warning_risk = 0.75
    breach_risk = 0.90

    # Retrieve session details to see if customer is high risk
    session_data = await get_session(session_id)
    is_high_risk = False
    
    if session_data:
        customer_id = session_data.get("customer_id")
        if customer_id:
            is_high_risk = await is_customer_high_churn_risk(customer_id)

    # Apply priority SLA if high risk
    if is_high_risk:
        warning_turn = 2
        breach_turn = 4
        warning_risk = 0.50
        breach_risk = 0.75
        # Update session memory to indicate Priority SLA is active
        try:
            await update_session(session_id, {"is_priority_sla": True})
        except Exception:
            pass

    # Query active load to calculate grace period extensions (Phase 33)
    try:
        from core.memory.session import get_redis
        r = get_redis()
        session_keys = []
        async for key in r.scan_iter(match="session:*"):
            if b"csat_pending" not in key:
                session_keys.append(key)
        session_count = len(session_keys)
    except Exception as exc:
        log.warning("sla_load_query_failed", error=str(exc))
        session_count = 0

    warning_offset, breach_offset = get_grace_offsets(session_count, is_high_risk)
    warning_turn += warning_offset
    breach_turn += breach_offset

    if warning_offset > 0 or breach_offset > 0:
        log.info(
            "sla_grace_period_extended",
            session_id=session_id,
            load=session_count,
            warning_offset=warning_offset,
            breach_offset=breach_offset,
            warning_turn=warning_turn,
            breach_turn=breach_turn
        )

    if turn_count > breach_turn or escalation_risk > breach_risk:
        status = "breached"
    elif turn_count > warning_turn or escalation_risk > warning_risk:
        status = "warning"
    else:
        status = "ok"

    log.info("sla_compliance_checked", session_id=session_id, turn_count=turn_count, escalation_risk=escalation_risk, status=status, is_priority=is_high_risk)
    return status
