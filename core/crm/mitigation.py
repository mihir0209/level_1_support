"""CSAT-Triggered Auto-Mitigation Playbook Recommendation Engine & Retry Orchestrator (Phases 34-36)."""

from __future__ import annotations

import json
import uuid
import time
import asyncio
import structlog
from datetime import datetime, timedelta

log = structlog.get_logger(__name__)


async def generate_csat_mitigation(
    session_id: str,
    customer_id: str | None,
    satisfaction: float,
    intent: str | None
) -> None:
    """
    Analyzes low CSAT satisfaction rating and creates an actionable mitigation proposal.
    Matches session intent with appropriate playbooks (coupon dispatch, callback, etc.).
    """
    # Only generate playbooks for ratings below 3.0
    if satisfaction >= 3.0:
        return

    import random
    variant = random.choice(["A", "B"])

    # Select playbook based on intent
    intent_lower = (intent or "").lower()
    if "return" in intent_lower or "refund" in intent_lower or "chargeback" in intent_lower:
        if variant == "A":
            suggested_playbook = "Dispatch apologetic coupon code"
            playbook_details = {
                "coupon_value": "15% OFF",
                "action": "offer_coupon",
                "reason": "Frustrated with returns or payments"
            }
        else:
            suggested_playbook = "Dispatch apologetic coupon code (Variant B - 20% OFF)"
            playbook_details = {
                "coupon_value": "20% OFF",
                "action": "offer_coupon",
                "reason": "Frustrated with returns or payments"
            }
    elif "defect" in intent_lower or "wrong" in intent_lower or "damage" in intent_lower:
        if variant == "A":
            suggested_playbook = "Initiate supervisor outbound callback"
            playbook_details = {
                "priority": "high",
                "action": "supervisor_callback",
                "reason": "Product defect or wrong order received"
            }
        else:
            suggested_playbook = "Initiate supervisor outbound callback (Variant B - Priority Express)"
            playbook_details = {
                "priority": "express",
                "action": "supervisor_callback",
                "reason": "Product defect or wrong order received",
                "callback_window": "1 hour"
            }
    else:
        if variant == "A":
            suggested_playbook = "Send custom apology email"
            playbook_details = {
                "priority": "standard",
                "action": "apology_email",
                "reason": "General support dissatisfaction"
            }
        else:
            suggested_playbook = "Send custom apology email (Variant B - High Priority Executive)"
            playbook_details = {
                "priority": "executive",
                "action": "apology_email",
                "reason": "General support dissatisfaction"
            }

    try:
        from core.db.pool import get_async_pool
        pool = get_async_pool()

        # Generate a valid session UUID
        try:
            session_uuid = uuid.UUID(session_id)
        except ValueError:
            session_uuid = uuid.uuid4()

        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO csat_mitigations (
                    id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status, variant_group
                ) VALUES (
                    gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    session_uuid,
                    customer_id,
                    satisfaction,
                    suggested_playbook,
                    json.dumps(playbook_details),
                    "pending",
                    variant
                )
            )
            log.info(
                "csat_mitigation_proposal_created",
                session_id=session_id,
                playbook=suggested_playbook,
                satisfaction=satisfaction
            )
    except Exception as exc:
        log.warning("failed_to_persist_csat_mitigation", session_id=session_id, error=str(exc))


async def execute_playbook_mitigation(mitigation_id: uuid.UUID) -> None:
    """
    Executes an approved CSAT mitigation playbook.
    Sends a POST request to configurations, or runs simulated mock dispatches.
    If execution fails, schedules retry with exponential backoff.
    """
    from config import settings
    from core.db.pool import get_async_pool
    from core.memory.session import get_redis
    import httpx

    pool = get_async_pool()
    mitigation_data = None

    # 1. Fetch mitigation details
    try:
        async with pool.connection() as conn:
            row = await (await conn.execute(
                """
                SELECT session_id, customer_id, satisfaction, suggested_playbook, playbook_details, retry_count
                FROM csat_mitigations
                WHERE id = %s
                """,
                (mitigation_id,)
            )).fetchone()
            if row:
                mitigation_data = {
                    "session_id": row[0],
                    "customer_id": row[1],
                    "satisfaction": row[2],
                    "suggested_playbook": row[3],
                    "playbook_details": row[4],
                    "retry_count": row[5] or 0
                }
    except Exception as fetch_err:
        log.warning("failed_to_fetch_mitigation_for_execution", id=str(mitigation_id), error=str(fetch_err))
        return

    if not mitigation_data:
        log.warning("mitigation_record_not_found_for_execution", id=str(mitigation_id))
        return

    executed_at = datetime.utcnow()
    execution_response = None
    execution_error = None

    webhook_url = settings.playbook_execution_webhook_url
    if webhook_url:
        # 2. Real dispatch to external webhook URL
        try:
            payload = {
                "mitigation_id": str(mitigation_id),
                "session_id": str(mitigation_data["session_id"]),
                "customer_id": mitigation_data["customer_id"],
                "satisfaction": mitigation_data["satisfaction"],
                "suggested_playbook": mitigation_data["suggested_playbook"],
                "playbook_details": mitigation_data["playbook_details"]
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                resp_json = None
                try:
                    resp_json = resp.json()
                except Exception:
                    pass
                execution_response = {
                    "status_code": resp.status_code,
                    "body": resp_json or resp.text
                }
                if resp.status_code >= 400:
                    execution_error = f"Webhook returned status code {resp.status_code}"
                log.info("playbook_webhook_dispatched", id=str(mitigation_id), status_code=resp.status_code)
        except Exception as http_err:
            execution_error = str(http_err)
            log.warning("playbook_webhook_failed", id=str(mitigation_id), error=str(http_err))
    else:
        # 3. Simulated execution if webhook is not configured
        execution_response = {
            "status": "simulated",
            "reason": "No webhook configured",
            "mocked_action": mitigation_data["suggested_playbook"],
            "mocked_details": mitigation_data["playbook_details"]
        }
        log.info("playbook_execution_simulated", id=str(mitigation_id), playbook=mitigation_data["suggested_playbook"])

    # 4. Save execution results & manage exponential backoff retries
    try:
        r = get_redis()
        async with pool.connection() as conn:
            if execution_error is not None:
                new_retry_count = mitigation_data["retry_count"] + 1
                if new_retry_count < 5:
                    delay = 60 * (2 ** new_retry_count)
                    next_retry_at = executed_at + timedelta(seconds=delay)
                    await conn.execute(
                        """
                        UPDATE csat_mitigations
                        SET executed_at = %s,
                            execution_response = %s,
                            execution_error = %s,
                            retry_count = %s,
                            next_retry_at = %s,
                            status = 'failed_retry'
                        WHERE id = %s
                        """,
                        (
                            executed_at,
                            json.dumps(execution_response) if execution_response else None,
                            execution_error,
                            new_retry_count,
                            next_retry_at,
                            mitigation_id
                        )
                    )
                    # Add to Redis retry sorted set
                    await r.zadd("mitigation:retry_queue", {str(mitigation_id): time.time() + delay})
                    log.info(
                        "playbook_execution_scheduled_retry",
                        id=str(mitigation_id),
                        retry_count=new_retry_count,
                        delay_seconds=delay
                    )
                else:
                    # Final failure
                    await conn.execute(
                        """
                        UPDATE csat_mitigations
                        SET executed_at = %s,
                            execution_response = %s,
                            execution_error = %s,
                            retry_count = %s,
                            next_retry_at = NULL,
                            status = 'failed'
                        WHERE id = %s
                        """,
                        (
                            executed_at,
                            json.dumps(execution_response) if execution_response else None,
                            execution_error,
                            new_retry_count,
                            mitigation_id
                        )
                    )
                    await r.zrem("mitigation:retry_queue", str(mitigation_id))
                    log.error("playbook_execution_permanently_failed", id=str(mitigation_id), attempts=new_retry_count)

                    try:
                        asyncio.create_task(
                            dispatch_playbook_failure_alert(
                                mitigation_id=mitigation_id,
                                session_id=mitigation_data["session_id"],
                                customer_id=mitigation_data["customer_id"],
                                satisfaction=mitigation_data["satisfaction"],
                                playbook=mitigation_data["suggested_playbook"],
                                details=mitigation_data["playbook_details"] or {},
                                error_msg=execution_error,
                                retry_count=new_retry_count
                            )
                        )
                    except Exception as task_err:
                        log.warning("failed_to_spawn_alert_task", id=str(mitigation_id), error=str(task_err))
            else:
                # Success
                await conn.execute(
                    """
                    UPDATE csat_mitigations
                    SET executed_at = %s,
                        execution_response = %s,
                        execution_error = NULL,
                        next_retry_at = NULL,
                        status = 'executed'
                    WHERE id = %s
                    """,
                    (
                        executed_at,
                        json.dumps(execution_response) if execution_response else None,
                        mitigation_id
                    )
                )
                await r.zrem("mitigation:retry_queue", str(mitigation_id))
                log.info("playbook_execution_completed", id=str(mitigation_id))
    except Exception as save_err:
        log.warning("failed_to_save_mitigation_execution_results", id=str(mitigation_id), error=str(save_err))


# ── Background Retry Queue Worker (Phase 36) ───────────────────────────

async def _retry_worker_loop() -> None:
    from core.memory.session import get_redis
    while True:
        try:
            r = get_redis()
            now_ts = time.time()
            expired_items = await r.zrangebyscore("mitigation:retry_queue", min=0, max=now_ts)
            for item_bytes in expired_items:
                item_str = item_bytes.decode("utf-8") if isinstance(item_bytes, bytes) else str(item_bytes)
                try:
                    mitigation_uuid = uuid.UUID(item_str)
                    # Remove early to avoid concurrent execution
                    await r.zrem("mitigation:retry_queue", item_str)
                    await execute_playbook_mitigation(mitigation_uuid)
                except Exception as item_err:
                    log.warning("retry_worker_failed_to_process_item", item=item_str, error=str(item_err))
        except Exception as loop_err:
            log.warning("retry_worker_loop_error", error=str(loop_err))
        await asyncio.sleep(10)  # Scan retry queue every 10 seconds


_retry_worker_task: asyncio.Task | None = None


def start_playbook_retry_worker() -> None:
    """Initialize the background task to poll the Redis mitigation retry queue."""
    global _retry_worker_task
    if _retry_worker_task is None:
        _retry_worker_task = asyncio.create_task(_retry_worker_loop())
        log.info("playbook_retry_worker_started")


async def dispatch_playbook_failure_alert(
    mitigation_id: uuid.UUID,
    session_id: uuid.UUID,
    customer_id: str | None,
    satisfaction: float,
    playbook: str,
    details: dict,
    error_msg: str | None,
    retry_count: int
) -> None:
    """
    Sends a real-time notification alert to a configured webhook (e.g. Slack/n8n)
    when a CSAT mitigation playbook execution permanently fails.
    """
    from config import settings
    import httpx

    webhook_url = settings.playbook_failure_alert_webhook_url
    if not webhook_url:
        log.info("playbook_failure_alert_skipped", reason="No alert webhook configured", id=str(mitigation_id))
        return

    payload = {
        "event": "playbook_execution_failed",
        "mitigation_id": str(mitigation_id),
        "session_id": str(session_id),
        "customer_id": customer_id,
        "satisfaction": satisfaction,
        "suggested_playbook": playbook,
        "playbook_details": details,
        "retry_count": retry_count,
        "last_error": error_msg,
        "failed_at": datetime.utcnow().isoformat()
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload)
            log.info("playbook_failure_alert_dispatched", id=str(mitigation_id), status_code=resp.status_code)
    except Exception as dispatch_err:
        log.warning("failed_to_dispatch_playbook_failure_alert", id=str(mitigation_id), error=str(dispatch_err))
