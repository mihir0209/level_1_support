"""Tests for Phase 38: Mitigation Playbook Analytics Endpoint."""

from __future__ import annotations

import json
import uuid
import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.fixture(autouse=True)
async def setup_db_pool_and_redis():
    """Autouse fixture to reset global database and Redis singletons to prevent loop mismatch."""
    import core.db.pool
    import core.memory.session
    # Force recreation on current running loop
    core.db.pool.async_pool = None
    core.db.pool.sync_pool = None
    core.memory.session._redis_client = None
    yield
    # Cleanup
    try:
        from core.memory.session import get_redis
        r = get_redis()
        await r.aclose()
    except Exception:
        pass
    core.memory.session._redis_client = None
    try:
        from core.db.pool import close_pools
        await close_pools()
    except Exception:
        pass


async def ensure_pool_opened():
    from core.db.pool import get_async_pool, open_async_pool
    # If the pool is closed or None, nullify it to force recreation
    import core.db.pool
    if core.db.pool.async_pool is not None and core.db.pool.async_pool._closed:
        core.db.pool.async_pool = None
    pool = get_async_pool()
    try:
        await open_async_pool()
    except Exception:
        pass
    return pool


async def seed_mitigations(pool):
    """Seed test mitigations for aggregate calculation checks."""
    session_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    mitigation_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

    async with pool.connection() as conn:
        for s_id in session_ids:
            await conn.execute(
                """
                INSERT INTO support_sessions (id, brand_id, customer_id, channel)
                VALUES (%s, 'boohoo_uk', 'cust_analytics', 'chat')
                """,
                (s_id,)
            )

        # 1. Executed coupon playbook (retries = 2)
        await conn.execute(
            """
            INSERT INTO csat_mitigations (id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status, retry_count)
            VALUES (%s, %s, 'cust_analytics', 1.0, 'Dispatch apologetic coupon code', '{}', 'executed', 2)
            """,
            (mitigation_ids[0], session_ids[0])
        )

        # 2. Failed callback playbook (retries = 5)
        await conn.execute(
            """
            INSERT INTO csat_mitigations (id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status, retry_count)
            VALUES (%s, %s, 'cust_analytics', 2.0, 'Initiate supervisor outbound callback', '{}', 'failed', 5)
            """,
            (mitigation_ids[1], session_ids[1])
        )

        # 3. Pending apology email (retries = 0)
        await conn.execute(
            """
            INSERT INTO csat_mitigations (id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status, retry_count)
            VALUES (%s, %s, 'cust_analytics', 2.5, 'Send custom apology email', '{}', 'pending', 0)
            """,
            (mitigation_ids[2], session_ids[2])
        )

    return session_ids, mitigation_ids


async def cleanup_mitigations(pool, session_ids, mitigation_ids):
    async with pool.connection() as conn:
        for m_id in mitigation_ids:
            await conn.execute("DELETE FROM csat_mitigations WHERE id = %s", (m_id,))
        for s_id in session_ids:
            await conn.execute("DELETE FROM support_sessions WHERE id = %s", (s_id,))


def test_mitigations_analytics_empty():
    """Verify mitigation analytics returns zeros when no data exists."""
    headers = {"X-Admin-Key": "l1support-demo-key-2026"}
    with TestClient(app) as client:
        response = client.get("/admin/analytics/mitigations", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] >= 0
        assert "status_counts" in data
        assert "playbook_stats" in data
        assert "avg_retries" in data


@pytest.mark.asyncio
async def test_mitigations_analytics_aggregation():
    """Verify aggregated computations logic (counts, average retries, success rates)."""
    pool = await ensure_pool_opened()

    session_ids, mitigation_ids = await seed_mitigations(pool)

    headers = {"X-Admin-Key": "l1support-demo-key-2026"}

    try:
        with TestClient(app) as client:
            response = client.get("/admin/analytics/mitigations", headers=headers)
            assert response.status_code == 200
            data = response.json()

            # Check total generated count includes our 3 items
            assert data["total_count"] >= 3

            # Check status counts
            status_counts = data["status_counts"]
            assert status_counts["executed"] >= 1
            assert status_counts["failed"] >= 1
            assert status_counts["pending"] >= 1

            # Check average retries (average of 2, 5, and maybe other items in DB)
            assert data["avg_retries"] > 0.0

            # Check playbook categories
            playbook_stats = data["playbook_stats"]
            assert len(playbook_stats) >= 3

            coupon_stat = next(p for p in playbook_stats if "coupon" in p["playbook_type"].lower())
            assert coupon_stat["total"] >= 1
            assert coupon_stat["executed"] >= 1
            assert coupon_stat["success_rate_percent"] == 100.0

            callback_stat = next(p for p in playbook_stats if "callback" in p["playbook_type"].lower())
            assert callback_stat["total"] >= 1
            assert callback_stat["failed"] >= 1
            assert callback_stat["success_rate_percent"] == 0.0

    finally:
        # Re-open pool if closed by TestClient shutdown lifespan
        fresh_pool = await ensure_pool_opened()
        await cleanup_mitigations(fresh_pool, session_ids, mitigation_ids)
