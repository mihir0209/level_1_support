"""Tests for Phase 36: CSAT Playbook Retry Queue with Exponential Backoff."""

from __future__ import annotations

import json
import uuid
import time
import pytest
import asyncio
from datetime import datetime, timedelta
from core.crm.mitigation import execute_playbook_mitigation, _retry_worker_loop


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
    pool = get_async_pool()
    try:
        await open_async_pool()
    except Exception:
        pass
    return pool


async def create_dummy_mitigation_with_retry(pool, mitigation_id: uuid.UUID, session_id: uuid.UUID, customer_id: str, retry_count: int = 0):
    """Creates a dummy mitigation record in PostgreSQL."""
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO support_sessions (id, brand_id, customer_id, channel)
            VALUES (%s, 'boohoo_uk', %s, 'chat')
            """,
            (session_id, customer_id)
        )
        await conn.execute(
            """
            INSERT INTO csat_mitigations (id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status, retry_count)
            VALUES (%s, %s, %s, 1.5, 'Dispatch apologetic coupon code', '{"coupon_value": "15%% OFF", "action": "offer_coupon"}', 'pending', %s)
            """,
            (mitigation_id, session_id, customer_id, retry_count)
        )


async def cleanup_dummy_mitigation(pool, mitigation_id: uuid.UUID, session_id: uuid.UUID):
    """Removes mitigation and support session records from PostgreSQL."""
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM csat_mitigations WHERE id = %s", (mitigation_id,))
        await conn.execute("DELETE FROM support_sessions WHERE id = %s", (session_id,))


@pytest.mark.asyncio
async def test_playbook_retry_scheduling(monkeypatch):
    """Verify that a failed webhook call schedules retry with correct backoff."""
    pool = await ensure_pool_opened()

    # Configure mock webhook URL to force http call
    from config import settings
    monkeypatch.setattr(settings, "playbook_execution_webhook_url", "https://n8n.workflow.engine/webhooks/csat-mitigate")

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "test_retry_sched"

    await create_dummy_mitigation_with_retry(pool, mitigation_id, session_id, customer_id, retry_count=0)

    # Force webhook HTTP failure (e.g. status 500)
    class MockResponse:
        status_code = 500
        def json(self):
            return {}
        @property
        def text(self):
            return "Server Error"

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    try:
        await execute_playbook_mitigation(mitigation_id)

        # Retrieve DB state
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT retry_count, next_retry_at, status FROM csat_mitigations WHERE id = %s",
                (mitigation_id,)
            )).fetchone()
            assert row is not None
            assert row[0] == 1              # retry_count incremented to 1
            assert row[1] is not None       # next_retry_at set
            assert row[2] == "failed_retry" # status updated

            # Check Redis retry sorted set
            from core.memory.session import get_redis
            r = get_redis()
            score = await r.zscore("mitigation:retry_queue", str(mitigation_id))
            assert score is not None
            assert score > time.time() + 110  # backoff delay = 120s
    finally:
        # Cleanup Redis & DB
        from core.memory.session import get_redis
        r = get_redis()
        await r.zrem("mitigation:retry_queue", str(mitigation_id))
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)


@pytest.mark.asyncio
async def test_playbook_max_retry_limit(monkeypatch):
    """Verify execution logic permanently fails after 5 retries."""
    pool = await ensure_pool_opened()

    from config import settings
    monkeypatch.setattr(settings, "playbook_execution_webhook_url", "https://n8n.workflow.engine/webhooks/csat-mitigate")

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "test_retry_max"

    # Start with retry_count = 4 (the 5th attempt will be 5)
    await create_dummy_mitigation_with_retry(pool, mitigation_id, session_id, customer_id, retry_count=4)

    # Force webhook HTTP failure (e.g. status 500)
    class MockResponse:
        status_code = 500
        def json(self):
            return {}
        @property
        def text(self):
            return "Server Error"

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    try:
        await execute_playbook_mitigation(mitigation_id)

        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT retry_count, next_retry_at, status FROM csat_mitigations WHERE id = %s",
                (mitigation_id,)
            )).fetchone()
            assert row is not None
            assert row[0] == 5           # retry_count set to 5
            assert row[1] is None        # next_retry_at set to NULL
            assert row[2] == "failed"    # status updated to failed

            # Verify NOT in Redis retry queue
            from core.memory.session import get_redis
            r = get_redis()
            score = await r.zscore("mitigation:retry_queue", str(mitigation_id))
            assert score is None
    finally:
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)


@pytest.mark.asyncio
async def test_playbook_retry_worker(monkeypatch):
    """Verify the background retry loop picks up and executes expired items."""
    pool = await ensure_pool_opened()

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "test_retry_worker"

    await create_dummy_mitigation_with_retry(pool, mitigation_id, session_id, customer_id)

    # Add to Redis retry sorted set with score in the past (expired)
    from core.memory.session import get_redis
    r = get_redis()
    await r.zadd("mitigation:retry_queue", {str(mitigation_id): time.time() - 100})

    # Track how many times execute_playbook_mitigation is called
    exec_called = []
    async def mock_execute(m_id):
        exec_called.append(m_id)

    monkeypatch.setattr("core.crm.mitigation.execute_playbook_mitigation", mock_execute)

    # Trigger worker scan once (using a task wrapper that completes instantly)
    # Mock loop sleep to stop after one iteration
    async def mock_sleep(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    try:
        # Run loop (will exit on CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await _retry_worker_loop()

        # Check execution triggered
        assert mitigation_id in exec_called

        # Check removed from Redis queue
        score = await r.zscore("mitigation:retry_queue", str(mitigation_id))
        assert score is None
    finally:
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)
