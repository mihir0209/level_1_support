"""Tests for Inactivity Auto-Escalator Background Worker."""

from __future__ import annotations

import json
import time
import pytest
from main import app
from core.orchestration.auto_escalator import (
    start_auto_escalator,
    stop_auto_escalator,
    scan_and_escalate_inactive_sessions,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_auto_escalator_lifecycle():
    """Verify background scanner task starts and stops cleanly."""
    await start_auto_escalator()
    await start_auto_escalator()  # double start no-op
    await stop_auto_escalator()


@pytest.mark.asyncio
async def test_scan_and_escalate_inactive_sessions(monkeypatch):
    """Verify that sessions inactive > 30 mins are marked as escalated and dispatch webhooks."""
    now = time.time()
    db_store = {
        "session:active_1": json.dumps({
            "session_id": "active_1",
            "brand_id": "brand_a",
            "outcome": "active",
            "updated_at": now - 100  # only 100 seconds inactive
        }),
        "session:inactive_2": json.dumps({
            "session_id": "inactive_2",
            "brand_id": "brand_b",
            "outcome": "active",
            "updated_at": now - 2000  # 2000 seconds inactive (over 30 mins)
        }),
        "session:already_resolved": json.dumps({
            "session_id": "resolved_3",
            "brand_id": "brand_a",
            "outcome": "resolved",
            "updated_at": now - 3000
        })
    }

    class MockRedis:
        async def scan_iter(self, match):
            for k in db_store.keys():
                yield k
        async def get(self, key):
            return db_store.get(key)
        async def setex(self, key, ttl, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.orchestration.auto_escalator.get_redis", lambda: MockRedis())

    # Track alert dispatches
    dispatches = []
    async def mock_dispatch(session_id, brand_id, elapsed):
        dispatches.append((session_id, brand_id))

    monkeypatch.setattr("core.orchestration.auto_escalator.dispatch_sms_alert", mock_dispatch)

    # Run scanner
    await scan_and_escalate_inactive_sessions()

    # Assert active_1 is still active
    s1 = json.loads(db_store["session:active_1"])
    assert s1.get("outcome") == "active"

    # Assert inactive_2 is now escalated
    s2 = json.loads(db_store["session:inactive_2"])
    assert s2.get("outcome") == "escalated"
    assert s2.get("sla_status") == "breached"

    # Assert resolved_3 is untouched
    s3 = json.loads(db_store["session:already_resolved"])
    assert s3.get("outcome") == "resolved"

    # Assert alert was dispatched for inactive_2
    assert len(dispatches) == 1
    assert dispatches[0] == ("inactive_2", "brand_b")


@pytest.mark.asyncio
async def test_sms_webhook_delivery():
    """Verify endpoint receives alerts and returns 200 delivered status."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/admin/notifications/sms",
            json={
                "session_id": "sess_123",
                "brand_id": "brand_x",
                "inactivity_minutes": 35.0,
                "message": "alert details"
            },
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        assert res.json()["status"] == "delivered"
