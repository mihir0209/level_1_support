"""Tests for Phase 37: Multi-Channel Live Notification Alerts on Playbook Failures."""

from __future__ import annotations

import json
import uuid
import asyncio
import pytest
import httpx
from core.crm.mitigation import execute_playbook_mitigation, dispatch_playbook_failure_alert


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
async def test_failure_alert_skipped_when_unset(monkeypatch):
    """Verify that failure alert is skipped gracefully if webhook URL is not configured."""
    pool = await ensure_pool_opened()

    from config import settings
    monkeypatch.setattr(settings, "playbook_failure_alert_webhook_url", "")

    # We track if httpx client post was called
    post_calls = []
    async def mock_post(*args, **kwargs):
        post_calls.append(args)
        class MockResponse:
            status_code = 200
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "alert_skipped"

    await create_dummy_mitigation_with_retry(pool, mitigation_id, session_id, customer_id, retry_count=4)

    # Force execution error to trigger final failure
    monkeypatch.setattr(settings, "playbook_execution_webhook_url", "https://mock.webhook")
    class FailResponse:
        status_code = 500
        def json(self):
            return {}
        @property
        def text(self):
            return "Internal Error"

    monkeypatch.setattr("httpx.AsyncClient.post", lambda *args, **kwargs: asyncio.sleep(0.01, result=FailResponse()))

    try:
        await execute_playbook_mitigation(mitigation_id)

        # Give background tasks a brief moment to run
        await asyncio.sleep(0.1)

        # No failure alert was posted since URL is unset
        assert len(post_calls) == 0
    finally:
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)


@pytest.mark.asyncio
async def test_failure_alert_dispatch_success(monkeypatch):
    """Verify that failure alert webhook is called with structured payload upon permanent failure."""
    pool = await ensure_pool_opened()

    from config import settings
    alert_url = "https://alert.system/webhook-failed"
    monkeypatch.setattr(settings, "playbook_failure_alert_webhook_url", alert_url)

    # Track requests
    dispatched_payloads = []
    async def mock_post(client_self, url, json=None, **kwargs):
        if url == alert_url:
            dispatched_payloads.append(json)
        class MockResponse:
            status_code = 200
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "alert_success"

    await create_dummy_mitigation_with_retry(pool, mitigation_id, session_id, customer_id, retry_count=4)

    # Mock execute_playbook_mitigation to simulate final failure by returning a webhook failure
    # Ensure playbook_execution_webhook_url is set so it uses real webhook dispatch code path
    monkeypatch.setattr(settings, "playbook_execution_webhook_url", "https://execute.webhook")
    
    # We want post to return 500 when it hits execution webhook, but 200 when it hits alert webhook
    async def mock_double_post(client_self, url, json=None, **kwargs):
        if url == alert_url:
            dispatched_payloads.append(json)
            class MockResponse200:
                status_code = 200
            return MockResponse200()
        else:
            class MockResponse500:
                status_code = 500
                def json(self):
                    return {}
                @property
                def text(self):
                    return "Server Error"
            return MockResponse500()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_double_post)

    try:
        await execute_playbook_mitigation(mitigation_id)

        # Allow spawned background task to run
        for _ in range(10):
            await asyncio.sleep(0.02)
            if len(dispatched_payloads) > 0:
                break

        assert len(dispatched_payloads) == 1
        payload = dispatched_payloads[0]
        assert payload["event"] == "playbook_execution_failed"
        assert payload["mitigation_id"] == str(mitigation_id)
        assert payload["session_id"] == str(session_id)
        assert payload["customer_id"] == customer_id
        assert payload["retry_count"] == 5
        assert "500" in payload["last_error"]
    finally:
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)


@pytest.mark.asyncio
async def test_failure_alert_resilient_to_dispatch_errors(monkeypatch):
    """Verify failure alert webhook errors do not block or crash execution flows."""
    pool = await ensure_pool_opened()

    from config import settings
    alert_url = "https://alert.system/webhook-failed"
    monkeypatch.setattr(settings, "playbook_failure_alert_webhook_url", alert_url)

    # Force alert webhook to throw a connection exception
    async def mock_error_post(client_self, url, json=None, **kwargs):
        if url == alert_url:
            raise httpx.ConnectError("Connection refused")
        else:
            class MockResponse500:
                status_code = 500
                def json(self):
                    return {}
                @property
                def text(self):
                    return "Server Error"
            return MockResponse500()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_error_post)

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "alert_error_handling"

    await create_dummy_mitigation_with_retry(pool, mitigation_id, session_id, customer_id, retry_count=4)
    monkeypatch.setattr(settings, "playbook_execution_webhook_url", "https://execute.webhook")

    try:
        # Run execution. It should successfully finish, even if alert task raises exception
        await execute_playbook_mitigation(mitigation_id)

        # Give it a moment to run task
        await asyncio.sleep(0.1)

        # Ensure status is successfully updated to 'failed' in DB
        async with pool.connection() as conn:
            status = await (await conn.execute(
                "SELECT status FROM csat_mitigations WHERE id = %s",
                (mitigation_id,)
            )).fetchone()
            assert status[0] == "failed"
    finally:
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)
