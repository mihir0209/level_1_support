"""Tests for Phase 39: Mitigation Playbook A/B Optimizer."""

from __future__ import annotations

import json
import uuid
import pytest
from fastapi.testclient import TestClient
from main import app
from core.crm.mitigation import generate_csat_mitigation


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
    import core.db.pool
    if core.db.pool.async_pool is not None and core.db.pool.async_pool._closed:
        core.db.pool.async_pool = None
    pool = get_async_pool()
    try:
        await open_async_pool()
    except Exception:
        pass
    return pool


@pytest.mark.asyncio
async def test_ab_variant_allocation():
    """Verify that generate_csat_mitigation allocates A or B variant randomly and persists it."""
    pool = await ensure_pool_opened()

    session_id = uuid.uuid4()
    # Create support session first
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO support_sessions (id, brand_id, customer_id, channel)
            VALUES (%s, 'boohoo_uk', 'cust_ab', 'chat')
            """,
            (session_id,)
        )

    try:
        # Generate mitigation - should randomly split A or B
        await generate_csat_mitigation(
            session_id=str(session_id),
            customer_id="cust_ab",
            satisfaction=1.5,
            intent="I want a refund on my purchase"
        )

        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT variant_group, suggested_playbook, recovery_converted FROM csat_mitigations WHERE session_id = %s",
                (session_id,)
            )).fetchone()

            assert row is not None
            variant = row[0]
            playbook_name = row[1]
            converted = row[2]

            assert variant in ["A", "B"]
            assert converted is False
            if variant == "A":
                assert "Variant B" not in playbook_name
            else:
                assert "Variant B" in playbook_name

    finally:
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM csat_mitigations WHERE session_id = %s", (session_id,))
            await conn.execute("DELETE FROM support_sessions WHERE id = %s", (session_id,))


@pytest.mark.asyncio
async def test_convert_mitigation_endpoint():
    """Verify mitigation conversion endpoint updates recovery_converted state successfully."""
    pool = await ensure_pool_opened()

    session_id = uuid.uuid4()
    mitigation_id = uuid.uuid4()

    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO support_sessions (id, brand_id, customer_id, channel)
            VALUES (%s, 'boohoo_uk', 'cust_ab', 'chat')
            """,
            (session_id,)
        )
        await conn.execute(
            """
            INSERT INTO csat_mitigations (id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status, variant_group, recovery_converted)
            VALUES (%s, %s, 'cust_ab', 1.0, 'Test Playbook', '{}', 'executed', 'B', FALSE)
            """,
            (mitigation_id, session_id)
        )

    headers = {"X-Admin-Key": "l1support-demo-key-2026"}

    try:
        with TestClient(app) as client:
            response = client.post(f"/admin/mitigations/{mitigation_id}/convert", headers=headers)
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "success"
            assert data["recovery_converted"] is True

            # Verify in Database
            async with pool.connection() as conn:
                row = await (await conn.execute(
                    "SELECT recovery_converted FROM csat_mitigations WHERE id = %s",
                    (mitigation_id,)
                )).fetchone()
                assert row[0] is True

    finally:
        # Re-open pool if closed by TestClient shutdown
        fresh_pool = await ensure_pool_opened()
        async with fresh_pool.connection() as conn:
            await conn.execute("DELETE FROM csat_mitigations WHERE id = %s", (mitigation_id,))
            await conn.execute("DELETE FROM support_sessions WHERE id = %s", (session_id,))


@pytest.mark.asyncio
async def test_ab_analytics_aggregates():
    """Verify aggregated computations logic for Variant A vs Variant B."""
    pool = await ensure_pool_opened()

    session_ids = [uuid.uuid4(), uuid.uuid4()]
    mitigation_ids = [uuid.uuid4(), uuid.uuid4()]

    async with pool.connection() as conn:
        for s_id in session_ids:
            await conn.execute(
                """
                INSERT INTO support_sessions (id, brand_id, customer_id, channel)
                VALUES (%s, 'boohoo_uk', 'cust_ab_analytics', 'chat')
                """,
                (s_id,)
            )

        # Seeding Variant A: executed and converted
        await conn.execute(
            """
            INSERT INTO csat_mitigations (id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status, variant_group, recovery_converted)
            VALUES (%s, %s, 'cust_ab_analytics', 1.0, 'Test Playbook A', '{}', 'executed', 'A', TRUE)
            """,
            (mitigation_ids[0], session_ids[0])
        )

        # Seeding Variant B: executed and not converted
        await conn.execute(
            """
            INSERT INTO csat_mitigations (id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status, variant_group, recovery_converted)
            VALUES (%s, %s, 'cust_ab_analytics', 2.0, 'Test Playbook B', '{}', 'executed', 'B', FALSE)
            """,
            (mitigation_ids[1], session_ids[1])
        )

    headers = {"X-Admin-Key": "l1support-demo-key-2026"}

    try:
        with TestClient(app) as client:
            response = client.get("/admin/analytics/mitigations", headers=headers)
            assert response.status_code == 200
            data = response.json()

            ab_stats = data["ab_stats"]
            assert "A" in ab_stats
            assert "B" in ab_stats

            assert ab_stats["A"]["executed"] >= 1
            assert ab_stats["A"]["converted"] >= 1
            assert ab_stats["A"]["conversion_rate_percent"] > 0.0

            assert ab_stats["B"]["executed"] >= 1
            assert ab_stats["B"]["converted"] >= 0

    finally:
        fresh_pool = await ensure_pool_opened()
        async with fresh_pool.connection() as conn:
            for m_id in mitigation_ids:
                await conn.execute("DELETE FROM csat_mitigations WHERE id = %s", (m_id,))
            for s_id in session_ids:
                await conn.execute("DELETE FROM support_sessions WHERE id = %s", (s_id,))
