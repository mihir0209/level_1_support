"""Tests for Phase 35: CSAT Mitigation Playbook Execution Orchestration."""

from __future__ import annotations

import json
import uuid
import pytest
import httpx
from fastapi.testclient import TestClient
from main import app
from core.crm.mitigation import execute_playbook_mitigation


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


async def create_dummy_mitigation(pool, mitigation_id: uuid.UUID, session_id: uuid.UUID, customer_id: str):
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
            INSERT INTO csat_mitigations (id, session_id, customer_id, satisfaction, suggested_playbook, playbook_details, status)
            VALUES (%s, %s, %s, 1.5, 'Dispatch apologetic coupon code', '{"coupon_value": "15%% OFF", "action": "offer_coupon"}', 'pending')
            """,
            (mitigation_id, session_id, customer_id)
        )


async def cleanup_dummy_mitigation(pool, mitigation_id: uuid.UUID, session_id: uuid.UUID):
    """Removes mitigation and support session records from PostgreSQL."""
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM csat_mitigations WHERE id = %s", (mitigation_id,))
        await conn.execute("DELETE FROM support_sessions WHERE id = %s", (session_id,))


@pytest.mark.asyncio
async def test_playbook_execution_simulated(monkeypatch):
    """Verify execution logic when no webhook URL is configured."""
    pool = await ensure_pool_opened()

    # Clear setting
    from config import settings
    monkeypatch.setattr(settings, "playbook_execution_webhook_url", "")

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "test_execution_sim"

    await create_dummy_mitigation(pool, mitigation_id, session_id, customer_id)

    try:
        # Run execution
        await execute_playbook_mitigation(mitigation_id)

        # Check DB updates
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT executed_at, execution_error, execution_response FROM csat_mitigations WHERE id = %s",
                (mitigation_id,)
            )).fetchone()
            assert row is not None
            assert row[0] is not None  # executed_at
            assert row[1] is None      # execution_error
            assert row[2]["status"] == "simulated"
    finally:
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)


@pytest.mark.asyncio
async def test_playbook_execution_webhook_success(monkeypatch):
    """Verify execution logic when webhook URL is configured and succeeds (200)."""
    pool = await ensure_pool_opened()

    # Configure mock webhook URL
    from config import settings
    webhook_url = "https://n8n.workflow.engine/webhooks/csat-mitigate"
    monkeypatch.setattr(settings, "playbook_execution_webhook_url", webhook_url)

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "test_execution_webhook_ok"

    await create_dummy_mitigation(pool, mitigation_id, session_id, customer_id)

    # Mock HTTPX response
    class MockResponse:
        status_code = 200
        def json(self):
            return {"status": "dispatched", "message": "Playbook processed"}
        @property
        def text(self):
            return '{"status": "dispatched", "message": "Playbook processed"}'

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    try:
        await execute_playbook_mitigation(mitigation_id)

        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT executed_at, execution_error, execution_response FROM csat_mitigations WHERE id = %s",
                (mitigation_id,)
            )).fetchone()
            assert row is not None
            assert row[0] is not None
            assert row[1] is None
            assert row[2]["status_code"] == 200
            assert row[2]["body"]["status"] == "dispatched"
    finally:
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)


@pytest.mark.asyncio
async def test_playbook_execution_webhook_failure(monkeypatch):
    """Verify execution logic when webhook URL fails (500)."""
    pool = await ensure_pool_opened()

    from config import settings
    webhook_url = "https://n8n.workflow.engine/webhooks/csat-mitigate"
    monkeypatch.setattr(settings, "playbook_execution_webhook_url", webhook_url)

    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    customer_id = "test_execution_webhook_fail"

    await create_dummy_mitigation(pool, mitigation_id, session_id, customer_id)

    # Mock HTTPX response failing
    class MockResponse:
        status_code = 500
        def json(self):
            raise ValueError()
        @property
        def text(self):
            return "Internal Server Error"

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    try:
        await execute_playbook_mitigation(mitigation_id)

        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT executed_at, execution_error, execution_response FROM csat_mitigations WHERE id = %s",
                (mitigation_id,)
            )).fetchone()
            assert row is not None
            assert row[0] is not None
            assert "500" in row[1]
            assert row[2]["status_code"] == 500
    finally:
        await cleanup_dummy_mitigation(pool, mitigation_id, session_id)


def test_api_action_mitigation_execution(monkeypatch):
    """Verify POST act_on_mitigation triggers execution when status is approved."""
    # Mock execute_playbook_mitigation to track if called
    called_ids = []
    async def mock_execute(mitigation_id):
        called_ids.append(mitigation_id)

    monkeypatch.setattr("core.crm.mitigation.execute_playbook_mitigation", mock_execute)

    headers = {"X-Admin-Key": "l1support-demo-key-2026"}
    client = TestClient(app)

    # Setup dummy database helper
    mitigation_id = uuid.uuid4()
    session_id = uuid.uuid4()
    
    # We must patch get_async_pool to return mock connection/pool to prevent actual Postgres updates
    class MockConn:
        async def execute(self, query, params=None):
            return self
        async def fetchone(self):
            return (mitigation_id,)
    class MockPool:
        def connection(self):
            class AsyncContext:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return AsyncContext()

    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: MockPool())

    # 1. Action Approve -> should trigger execute_playbook_mitigation
    resp = client.post(
        f"/admin/mitigations/{mitigation_id}/action",
        json={"action": "approve"},
        headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "approved"
    assert mitigation_id in called_ids

    # 2. Action Dismiss -> should NOT trigger execute_playbook_mitigation
    called_ids.clear()
    resp2 = client.post(
        f"/admin/mitigations/{mitigation_id}/action",
        json={"action": "dismiss"},
        headers=headers
    )
    assert resp2.status_code == 200
    assert resp2.json()["new_status"] == "dismissed"
    assert len(called_ids) == 0
