"""Customer History Enrichment.

Retrieves past session summaries for returning customers from PostgreSQL,
caches them in Redis, and formats them for injection into the agent's
system prompt via the context engine.
"""

from __future__ import annotations

import json
import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# HISTORY RETRIEVAL
# ═══════════════════════════════════════════════════════════════════════════

CACHE_KEY_PREFIX = "customer_history"
CACHE_TTL = 1800  # 30 minutes


async def get_customer_history(customer_id: str, limit: int = 5) -> list[dict]:
    """
    Retrieve the last *limit* support sessions for a customer.

    Resolution order:
      1. Redis cache  (fast path, TTL 30 min)
      2. PostgreSQL   (authoritative source)
      3. Empty list   (graceful degradation)
    """
    if not customer_id:
        return []

    # 1. Check Redis cache
    r = get_redis()
    cache_key = f"{CACHE_KEY_PREFIX}:{customer_id}"
    cached = await r.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. Query PostgreSQL
    history = await _fetch_history_from_db(customer_id, limit)

    # 3. Cache the result (even empty list → avoids repeated DB hits)
    if history is not None:
        await r.setex(cache_key, CACHE_TTL, json.dumps(history))

    return history or []


async def _fetch_history_from_db(customer_id: str, limit: int) -> list[dict] | None:
    """Query support_sessions for past sessions belonging to this customer."""
    try:
        from core.db.pool import get_async_pool

        pool = get_async_pool()
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT id, brand_id, intent, outcome, satisfaction,
                           turn_count, started_at, ended_at
                    FROM support_sessions
                    WHERE customer_id = %s
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (customer_id, limit),
                )
            ).fetchall()

            return [
                {
                    "session_id": str(row[0]),
                    "brand_id": row[1],
                    "intent": row[2],
                    "outcome": row[3],
                    "satisfaction": float(row[4]) if row[4] is not None else None,
                    "turn_count": row[5],
                    "started_at": row[6].isoformat() if row[6] else None,
                    "ended_at": row[7].isoformat() if row[7] else None,
                }
                for row in rows
            ]
    except Exception as exc:
        log.warning("customer_history_db_fetch_failed", customer_id=customer_id, error=str(exc))
        return None


# ═══════════════════════════════════════════════════════════════════════════
# PROMPT FORMATTING
# ═══════════════════════════════════════════════════════════════════════════

def format_history_block(history: list[dict]) -> str:
    """
    Format a customer's interaction history into a context block suitable
    for injection into the agent's system prompt.

    Returns an empty string if the history list is empty.
    """
    if not history:
        return ""

    lines = [
        f"## Returning Customer Context",
        f"This customer has interacted {len(history)} time(s) previously:",
    ]

    for entry in history:
        date_str = entry.get("started_at", "unknown")
        if date_str and "T" in str(date_str):
            date_str = str(date_str).split("T")[0]

        intent = entry.get("intent") or "unknown"
        outcome = entry.get("outcome") or "active"
        satisfaction = entry.get("satisfaction")
        sat_str = f"{satisfaction}/5" if satisfaction is not None else "N/A"

        lines.append(f"- {date_str}: Intent={intent}, Outcome={outcome}, Satisfaction={sat_str}")

    # Guidance for the agent
    has_escalations = any(e.get("outcome") == "escalated" for e in history)
    has_low_sat = any(
        (e.get("satisfaction") or 5.0) < 3.0 for e in history
    )

    if has_escalations or has_low_sat:
        lines.append("")
        lines.append(
            "⚠️ This customer has had negative past experiences. "
            "Be extra empathetic, proactive, and prioritise first-contact resolution."
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# CACHE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

async def invalidate_customer_cache(customer_id: str) -> None:
    """Evict a customer's history cache (e.g. after a session ends)."""
    r = get_redis()
    await r.delete(f"{CACHE_KEY_PREFIX}:{customer_id}")
    log.info("customer_history_cache_invalidated", customer_id=customer_id)
