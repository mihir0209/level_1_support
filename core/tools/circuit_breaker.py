"""Tool API circuit breaker registry."""

from __future__ import annotations

import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)


async def track_tool_failure(tool_name: str) -> bool:
    """
    Increment consecutive failure count for a tool in Redis.
    If failures reach >= 3, trip the breaker for 10 minutes (600s).
    Returns True if tripped, False otherwise.
    """
    try:
        r = get_redis()
        fail_key = f"circuit_breaker:{tool_name}:failures"
        fails = await r.incr(fail_key)
        
        if fails >= 3:
            status_key = f"circuit_breaker:{tool_name}:status"
            await r.setex(status_key, 600, "tripped")
            log.warning("tool_circuit_breaker_tripped", tool_name=tool_name, consecutive_failures=fails)
            return True
    except Exception as e:
        log.warning("track_tool_failure_redis_failed", tool_name=tool_name, error=str(e))
    return False


async def track_tool_success(tool_name: str) -> None:
    """Reset consecutive failure counter to 0 upon successful execution."""
    try:
        r = get_redis()
        fail_key = f"circuit_breaker:{tool_name}:failures"
        await r.set(fail_key, 0)
    except Exception as e:
        log.warning("track_tool_success_redis_failed", tool_name=tool_name, error=str(e))


async def is_tool_tripped(tool_name: str) -> bool:
    """Check if the tool's circuit breaker is currently tripped in Redis."""
    try:
        r = get_redis()
        status_key = f"circuit_breaker:{tool_name}:status"
        status = await r.get(status_key)
        return status == "tripped"
    except Exception as e:
        log.warning("check_tool_tripped_redis_failed", tool_name=tool_name, error=str(e))
    return False


async def reset_tool_breaker(tool_name: str) -> None:
    """Manually restore a tool breaker to Active status."""
    try:
        r = get_redis()
        fail_key = f"circuit_breaker:{tool_name}:failures"
        status_key = f"circuit_breaker:{tool_name}:status"
        await r.delete(fail_key, status_key)
        log.info("tool_circuit_breaker_manually_reset", tool_name=tool_name)
    except Exception as e:
        log.warning("reset_tool_breaker_redis_failed", tool_name=tool_name, error=str(e))


async def get_all_breakers() -> list[dict]:
    """Retrieve the status and failure count for all tools."""
    monitored_tools = [
        "order_lookup", "shipment_track", "return_init", "refund_status",
        "cancel_order", "email_label", "promo_validate", "faq_search",
        "policy_search", "size_guide", "account_status", "escalate"
    ]
    
    breakers = []
    try:
        r = get_redis()
        for t in monitored_tools:
            fail_key = f"circuit_breaker:{t}:failures"
            status_key = f"circuit_breaker:{t}:status"
            
            raw_fails = await r.get(fail_key)
            status = await r.get(status_key)
            
            fails = int(raw_fails) if raw_fails else 0
            is_tripped = (status == "tripped")
            
            breakers.append({
                "tool_name": t,
                "failures": fails,
                "status": "tripped" if is_tripped else "active",
            })
    except Exception as e:
        log.warning("get_all_breakers_redis_failed", error=str(e))
        
    return breakers
