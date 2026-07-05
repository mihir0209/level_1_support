"""Session Inactivity Auto-Escalator Background Worker."""

from __future__ import annotations

import asyncio
import json
import time
import httpx
import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)

_escalator_task: asyncio.Task | None = None
INACTIVITY_THRESHOLD_SECONDS = 1800  # 30 minutes


async def start_auto_escalator() -> None:
    """Start the periodic session inactivity scanner background task."""
    global _escalator_task
    if _escalator_task is not None:
        log.warning("auto_escalator_already_running")
        return

    _escalator_task = asyncio.create_task(_escalator_loop())
    log.info("auto_escalator_started")


async def stop_auto_escalator() -> None:
    """Stop the periodic session inactivity scanner background task."""
    global _escalator_task
    if _escalator_task is None:
        return

    _escalator_task.cancel()
    try:
        await _escalator_task
    except asyncio.CancelledError:
        pass
    _escalator_task = None
    log.info("auto_escalator_stopped")


async def _escalator_loop() -> None:
    """Scan sessions every 30 seconds for inactivity and trigger alert notifications."""
    await asyncio.sleep(2)
    while True:
        try:
            await scan_and_escalate_inactive_sessions()
        except Exception as e:
            log.error("auto_escalator_loop_failed", error=str(e))
        await asyncio.sleep(30)


async def scan_and_escalate_inactive_sessions() -> None:
    """Search Redis for inactive open sessions, mark them as escalated, and send webhook notifications."""
    r = get_redis()
    keys = []
    async for key in r.scan_iter(match="session:*"):
        keys.append(key)

    now = time.time()
    for key in keys:
        raw = await r.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        outcome = data.get("outcome", "active")
        # Only escalate sessions that are not resolved or already escalated
        if outcome in ("resolved", "escalated"):
            continue

        updated_at = data.get("updated_at", now)
        elapsed = now - updated_at

        if elapsed >= INACTIVITY_THRESHOLD_SECONDS:
            session_id = data.get("session_id")
            brand_id = data.get("brand_id")
            channel = data.get("channel", "chat")
            turn_count = data.get("turn_count", 0)

            # Update session state to escalated
            data["outcome"] = "escalated"
            data["sla_status"] = "breached"
            data["updated_at"] = now
            await r.setex(key, 7200, json.dumps(data))

            log.warning("session_auto_escalated_due_to_inactivity", session_id=session_id, elapsed_seconds=elapsed)

            # Trigger simulated SMS/on-call webhook
            await dispatch_sms_alert(session_id, brand_id, elapsed)

            # Broadcast session update via WebSocket (Phase 21)
            try:
                from main import ws_manager
                await ws_manager.broadcast({
                    "event": "session_updated",
                    "data": {
                        "session_id": session_id,
                        "brand_id": brand_id,
                        "channel": channel,
                        "turn_count": turn_count,
                        "outcome": "escalated",
                        "escalation_risk": data.get("escalation_risk", 0.0),
                        "sla_status": "breached",
                        "is_takeover": False
                    }
                })
            except Exception as exc:
                log.warning("ws_broadcast_failed_during_auto_escalation", error=str(exc))


async def dispatch_sms_alert(session_id: str, brand_id: str, elapsed_seconds: float) -> None:
    """Send simulated SMS supervisor notification request to webhook endpoint."""
    url = "http://localhost:8000/admin/notifications/sms"
    payload = {
        "session_id": session_id,
        "brand_id": brand_id,
        "inactivity_minutes": round(elapsed_seconds / 60, 1),
        "message": f"Session {session_id} for brand {brand_id} has been inactive for {round(elapsed_seconds / 60, 1)} minutes. Auto-escalating to supervisor."
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, timeout=5.0)
            if res.status_code == 200:
                log.info("sms_alert_webhook_delivered", session_id=session_id)
            else:
                log.warning("sms_alert_webhook_bad_status", session_id=session_id, status_code=res.status_code)
    except Exception as exc:
        log.warning("sms_alert_webhook_network_error", session_id=session_id, error=str(exc))
