"""Tests for CSAT-Triggered Auto-Mitigation Playbook Dispatcher (Phase 34)."""

from __future__ import annotations

import json
import uuid
import pytest
from fastapi.testclient import TestClient
from main import app
from core.crm.mitigation import generate_csat_mitigation
from core.crm.csat import process_csat_response


async def ensure_pool_opened():
    import core.db.pool
    if core.db.pool.async_pool is not None and core.db.pool.async_pool._closed:
        core.db.pool.async_pool = None
    pool = core.db.pool.get_async_pool()
    try:
        await core.db.pool.open_async_pool()
    except Exception:
        pass
    return pool


async def create_dummy_support_session(pool, session_id: str, customer_id: str | None = None):
    """Create a dummy support session in PostgreSQL to satisfy the foreign key constraint."""
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO support_sessions (id, brand_id, customer_id, channel)
            VALUES (%s, 'boohoo_uk', %s, 'chat')
            """,
            (uuid.UUID(session_id), customer_id)
        )


@pytest.mark.asyncio
async def test_generate_csat_mitigation():
    """Verify different playbooks are selected depending on intent."""
    pool = await ensure_pool_opened()

    # Test 1: returns/refunds intent -> Coupon Playbook
    session_id = str(uuid.uuid4())
    customer_id = "test_cust_mitigation"
    await create_dummy_support_session(pool, session_id, customer_id)

    await generate_csat_mitigation(session_id, customer_id, 1.5, "return_pending")
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT suggested_playbook, playbook_details, status FROM csat_mitigations WHERE session_id = %s",
            (uuid.UUID(session_id),)
        )).fetchone()
        assert row is not None
        assert "coupon" in row[0].lower()
        details = row[1]
        assert details["action"] == "offer_coupon"
        assert row[2] == "pending"

        # Cleanup
        await conn.execute("DELETE FROM csat_mitigations WHERE session_id = %s", (uuid.UUID(session_id),))
        await conn.execute("DELETE FROM support_sessions WHERE id = %s", (uuid.UUID(session_id),))

    # Test 2: product defect/damage intent -> Supervisor Callback
    session_id_2 = str(uuid.uuid4())
    await create_dummy_support_session(pool, session_id_2, customer_id)
    await generate_csat_mitigation(session_id_2, customer_id, 2.0, "defect_damage")
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT suggested_playbook, playbook_details, status FROM csat_mitigations WHERE session_id = %s",
            (uuid.UUID(session_id_2),)
        )).fetchone()
        assert row is not None
        assert "callback" in row[0].lower()
        assert row[1]["action"] == "supervisor_callback"

        # Cleanup
        await conn.execute("DELETE FROM csat_mitigations WHERE session_id = %s", (uuid.UUID(session_id_2),))
        await conn.execute("DELETE FROM support_sessions WHERE id = %s", (uuid.UUID(session_id_2),))

    # Test 3: general inquiry intent -> Apology Email
    session_id_3 = str(uuid.uuid4())
    await create_dummy_support_session(pool, session_id_3, customer_id)
    await generate_csat_mitigation(session_id_3, customer_id, 2.5, "general_inquiry")
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT suggested_playbook, playbook_details, status FROM csat_mitigations WHERE session_id = %s",
            (uuid.UUID(session_id_3),)
        )).fetchone()
        assert row is not None
        assert "apology email" in row[0].lower()
        assert row[1]["action"] == "apology_email"

        # Cleanup
        await conn.execute("DELETE FROM csat_mitigations WHERE session_id = %s", (uuid.UUID(session_id_3),))
        await conn.execute("DELETE FROM support_sessions WHERE id = %s", (uuid.UUID(session_id_3),))


@pytest.mark.asyncio
async def test_csat_survey_response_mitigation_integration(monkeypatch):
    """Verify that process_csat_response registers mitigation when rating is low."""
    pool = await ensure_pool_opened()

    session_id = str(uuid.uuid4())
    customer_id = "mit_cust_integration"
    await create_dummy_support_session(pool, session_id, customer_id)

    # Setup mock active session
    from core.memory.session import get_redis, update_session
    r = get_redis()
    await update_session(session_id, {
        "brand_id": "boohoo_uk",
        "customer_id": customer_id,
        "intent": "returns_refunds",
        "empathy_mode": "thoughtful"
    })
    # Store customer ID in bytes format in Redis to prevent decode attribute errors
    await r.set(f"session:csat_pending:{session_id}", customer_id.encode())

    # Mock post_message so Chatwoot calls don't hit external APIs
    monkeypatch.setattr("channels.chat.chatwoot_client.post_message", lambda *args, **kwargs: None)

    # Submit high CSAT ("5") -> Should NOT create mitigation
    res = await process_csat_response(session_id, "5")
    assert res is True
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT id FROM csat_mitigations WHERE session_id = %s",
            (uuid.UUID(session_id),)
        )).fetchone()
        assert row is None

    # Reset pending survey
    await r.set(f"session:csat_pending:{session_id}", customer_id.encode())

    # Submit low CSAT ("2") -> Should create mitigation
    res2 = await process_csat_response(session_id, "2")
    assert res2 is True
    async with pool.connection() as conn:
        row2 = await (await conn.execute(
            "SELECT id, suggested_playbook, satisfaction FROM csat_mitigations WHERE session_id = %s",
            (uuid.UUID(session_id),)
        )).fetchone()
        assert row2 is not None
        assert row2[2] == 2.0

        # Cleanup
        await conn.execute("DELETE FROM csat_mitigations WHERE session_id = %s", (uuid.UUID(session_id),))
        await conn.execute("DELETE FROM support_sessions WHERE id = %s", (uuid.UUID(session_id),))


@pytest.mark.asyncio
async def test_mitigations_api_endpoints():
    """Verify GET and POST endpoints for mitigations using TestClient."""
    pool = await ensure_pool_opened()

    session_id = uuid.uuid4()
    customer_id = "api_mitigation_test"

    # Insert a fake pending record
    mitigation_id = uuid.uuid4()
    async with pool.connection() as conn:
        # We need a support session reference
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
            VALUES (%s, %s, %s, 1.0, 'Dispatch apologetic coupon code', '{}', 'pending')
            """,
            (mitigation_id, session_id, customer_id)
        )

    headers = {"X-Admin-Key": "l1support-demo-key-2026"}
    client = TestClient(app)

    # 1. GET pending mitigations
    response = client.get("/admin/mitigations/pending", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert any(item["id"] == str(mitigation_id) for item in data["items"])

    # 2. POST action (Dismiss)
    action_res = client.post(
        f"/admin/mitigations/{mitigation_id}/action",
        json={"action": "dismiss"},
        headers=headers
    )
    assert action_res.status_code == 200
    assert action_res.json()["new_status"] == "dismissed"

    # Verify DB updated
    async with pool.connection() as conn:
        status = await (await conn.execute(
            "SELECT status FROM csat_mitigations WHERE id = %s",
            (mitigation_id,)
        )).fetchone()
        assert status[0] == "dismissed"

    # 3. GET pending again -> should not contain dismissed mitigation
    response2 = client.get("/admin/mitigations/pending", headers=headers)
    assert response2.status_code == 200
    data2 = response2.json()
    assert not any(item["id"] == str(mitigation_id) for item in data2["items"])

    # Cleanup DB
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM csat_mitigations WHERE id = %s", (mitigation_id,))
        await conn.execute("DELETE FROM support_sessions WHERE id = %s", (session_id,))
