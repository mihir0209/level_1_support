"""
Three-tier memory layer.

Tier 1: Redis (working memory — session TTL, in-process state)
Tier 2: MemPalace (30-day verbatim session storage, 96.6% R@5 recall)
Tier 3: Graphiti (permanent temporal KG — long-term customer history)

Phase 1: Tier 1 (Redis) is fully implemented.
         Tier 2+3 stubs are ready for Phase 2 wiring.
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

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


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
# TIER 2: MEMPALACE — 30-Day Verbatim Memory (Phase 2 stub)
# ═══════════════════════════════════════════════════════════════════════════

class MemPalaceClient:
    """
    Stub for MemPalace integration.
    Phase 2: wire up the real mempalace SDK (29 MCP tools available).

    Hierarchy: Wing=brand_id, Room=customer_id, Drawer=session_id
    """

    async def remember(self, brand_id: str, customer_id: str, session_id: str, content: str) -> None:
        log.debug("mempalace_remember_stub", brand_id=brand_id, customer_id=customer_id)
        # Phase 2: mempalace.remember(content, wing=brand_id, room=customer_id, drawer=session_id)

    async def recall(self, brand_id: str, customer_id: str, query: str, k: int = 3) -> list[str]:
        log.debug("mempalace_recall_stub", brand_id=brand_id, customer_id=customer_id)
        # Phase 2: return await mempalace.recall(query, wing=brand_id, room=customer_id, k=k)
        return []


# ═══════════════════════════════════════════════════════════════════════════
# TIER 3: GRAPHITI — Permanent Temporal KG (Phase 2 stub)
# ═══════════════════════════════════════════════════════════════════════════

class GraphitiMemory:
    """
    Stub for Graphiti temporal knowledge graph integration.
    Phase 2: wire up graphiti-core with the FalkorDB backend.

    Stores facts like:
    - "Customer X contacted 3 times about order Y in 30 days"
    - "Customer X's preferred size is UK 10 (as of last 3 orders)"
    - "Customer X's PLT return initiated 2025-11-01, refund received 2025-11-21"
    """

    async def add_episode(
        self,
        customer_id: str,
        brand_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        log.debug("graphiti_add_episode_stub", customer_id=customer_id, brand_id=brand_id)
        # Phase 2: await graphiti.add_episode(content=content, metadata=metadata)

    async def get_recent_facts(
        self,
        customer_id: str,
        brand_id: str,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        log.debug("graphiti_recall_stub", customer_id=customer_id, brand_id=brand_id)
        # Phase 2: return await graphiti.search(f"customer:{customer_id}", ...)
        return []


# Module-level singletons
mempalace = MemPalaceClient()
graphiti_memory = GraphitiMemory()
