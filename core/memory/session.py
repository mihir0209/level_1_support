"""
Three-tier memory layer.

Tier 1: Redis (working memory — session TTL, in-process state)
Tier 2: MemPalace (30-day verbatim session storage — ChromaDB backed)
Tier 3: Graphiti (permanent temporal KG — FalkorDB backed)
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis
import structlog

from config import settings

log = structlog.get_logger(__name__)

SESSION_TTL = timedelta(hours=2)  # session expires after 2 hours of inactivity


# ═══════════════════════════════════════════════════════════════════════════
# TIER 1: REDIS — Working Memory
# ═══════════════════════════════════════════════════════════════════════════

_redis_client: "aioredis.Redis | None" = None


def get_redis() -> aioredis.Redis:
    """Return the shared Redis client singleton (one connection pool per process)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


class SessionMemory:
    """
    Redis-backed session state.
    Key: session:{session_id}
    Value: JSON-serialised session dict
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._key = f"session:{session_id}"

    async def load(self) -> dict[str, Any] | None:
        r = get_redis()
        raw = await r.get(self._key)
        if not raw:
            return None
        return json.loads(raw)

    async def save(self, data: dict[str, Any]) -> None:
        r = get_redis()
        await r.setex(
            self._key,
            int(SESSION_TTL.total_seconds()),
            json.dumps(data, default=str),
        )

    async def delete(self) -> None:
        r = get_redis()
        await r.delete(self._key)

    async def extend_ttl(self) -> None:
        r = get_redis()
        await r.expire(self._key, int(SESSION_TTL.total_seconds()))


async def create_session(brand_id: str, channel: str, customer_id: str | None = None) -> str:
    """Create a new session and persist its initial state to Redis."""
    session_id = str(uuid.uuid4())
    mem = SessionMemory(session_id)
    await mem.save({
        "session_id": session_id,
        "brand_id": brand_id,
        "channel": channel,
        "customer_id": customer_id,
        "messages": [],
        "turn_count": 0,
        "resolution_attempts": 0,
        "is_resolved": False,
    })
    log.info("session_created", session_id=session_id, brand_id=brand_id, channel=channel)
    return session_id


async def get_session(session_id: str) -> dict[str, Any] | None:
    mem = SessionMemory(session_id)
    return await mem.load()


async def update_session(session_id: str, updates: dict[str, Any]) -> None:
    mem = SessionMemory(session_id)
    current = await mem.load() or {}
    current.update(updates)
    await mem.save(current)


# ═══════════════════════════════════════════════════════════════════════════
# TIER 2 + TIER 3 — imported from dedicated modules
# ═══════════════════════════════════════════════════════════════════════════

from core.memory.mempalace_client import MemPalaceClient, mempalace  # noqa: E402
from core.memory.graphiti_client import GraphitiMemory, graphiti_memory  # noqa: E402

__all__ = [
    "SessionMemory",
    "create_session",
    "get_session",
    "update_session",
    "MemPalaceClient",
    "mempalace",
    "GraphitiMemory",
    "graphiti_memory",
]
