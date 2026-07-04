"""CRM Ticket synchronization adapter."""

from __future__ import annotations

import json
import time
import httpx
import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)


async def get_crm_config() -> dict:
    """Retrieve external CRM connection endpoint details."""
    r = get_redis()
    url = await r.get("crm:config:webhook_url") or "https://mock-crm.api.internal/tickets"
    token = await r.get("crm:config:auth_token") or "mock-token-abc-123"
    return {"webhook_url": url, "auth_token": token}


async def save_crm_config(url: str, token: str) -> None:
    """Save external CRM connection endpoint details."""
    r = get_redis()
    await r.set("crm:config:webhook_url", url)
    await r.set("crm:config:auth_token", token)
    log.info("crm_config_saved", webhook_url=url)


async def get_crm_sync_logs() -> list[dict]:
    """Retrieve history of CRM synchronization dispatches."""
    r = get_redis()
    raw = await r.lrange("crm:sync_logs", 0, 99)
    return [json.loads(x) for x in raw if x]


async def sync_session_to_crm(session_id: str, brand_id: str, session_data: dict) -> dict | None:
    """
    Constructs ticket payload and dispatches it to external ticketing CRM systems (e.g. Zendesk, Salesforce).
    Logs the transfer history to Redis.
    """
    config = await get_crm_config()
    webhook_url = config["webhook_url"]
    auth_token = config["auth_token"]

    # Gather data fields
    messages = session_data.get("messages", [])
    transcript = "\n".join([f"{m.get('role', 'user').upper()}: {m.get('content', '')}" for m in messages])

    payload = {
        "ticket": {
            "subject": f"L1 Support AI Ticket - {brand_id.upper()} - Session {session_id}",
            "description": f"AI-native conversation transcript:\n\n{transcript}",
            "custom_fields": {
                "session_id": session_id,
                "brand_id": brand_id,
                "turn_count": session_data.get("turn_count", 0),
                "outcome": session_data.get("outcome", "active"),
                "escalation_risk": session_data.get("escalation_risk", 0.0),
                "compliance_score": session_data.get("compliance_score", 1.0),
                "compliance_violations": session_data.get("compliance_violations"),
                "sla_status": session_data.get("sla_status", "ok"),
            }
        }
    }

    status = "success"
    error_msg = None
    t_start = time.monotonic()

    # Mock dispatcher to avoid true outgoing call failure during test suites
    if webhook_url == "https://mock-crm.api.internal/tickets" or "mock-crm" in webhook_url:
        # Simulated delay
        latency = 0.05
    else:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
                res = await client.post(webhook_url, json=payload, headers=headers)
                if res.status_code >= 400:
                    status = "failed"
                    error_msg = f"HTTP Error {res.status_code}: {res.text}"
        except Exception as e:
            status = "failed"
            error_msg = str(e)

    latency_ms = (time.monotonic() - t_start) * 1000

    log_entry = {
        "session_id": session_id,
        "brand_id": brand_id,
        "timestamp": int(time.time()),
        "status": status,
        "latency_ms": round(latency_ms, 1),
        "error": error_msg,
        "webhook_url": webhook_url
    }

    # Append to sync logs
    r = get_redis()
    await r.lpush("crm:sync_logs", json.dumps(log_entry))
    await r.ltrim("crm:sync_logs", 0, 99)

    log.info("crm_session_synced", session_id=session_id, status=status, error=error_msg)
    return log_entry
