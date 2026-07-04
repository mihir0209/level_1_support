"""Tests for Inbound Voice IVR Webhook and Router."""

from __future__ import annotations

import pytest
from main import app
from core.voice.ivr_router import (
    register_inbound_call,
    get_active_inbound_calls,
    update_inbound_call_digit,
    remove_inbound_call,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_ivr_calls_registration_and_updates(monkeypatch):
    """Verify that inbound call metadata gets correctly stored and removed in Redis."""
    db_store = {}

    class MockRedis:
        async def hset(self, name, key, val):
            db_store[key] = val
            return 1
        async def hget(self, name, key):
            return db_store.get(key)
        async def hgetall(self, name):
            return db_store
        async def hdel(self, name, key):
            if key in db_store:
                del db_store[key]
                return 1
            return 0

    monkeypatch.setattr("core.voice.ivr_router.get_redis", lambda: MockRedis())

    # Register inbound call
    await register_inbound_call("call_sid_123", "+12345678")
    assert "call_sid_123" in db_store

    # Update digit entered
    await update_inbound_call_digit("call_sid_123", "2")
    active = await get_active_inbound_calls()
    assert len(active) == 1
    assert active[0]["selected_digit"] == "2"

    # Remove call
    await remove_inbound_call("call_sid_123")
    assert "call_sid_123" not in db_store


@pytest.mark.asyncio
async def test_voice_ivr_endpoints(monkeypatch):
    """Verify webhooks respond with application/xml media headers and valid TwiML elements."""
    db_store = {}

    class MockRedis:
        async def hset(self, name, key, val):
            db_store[key] = val
            return 1
        async def hget(self, name, key):
            return db_store.get(key)
        async def hgetall(self, name):
            return db_store
        async def hdel(self, name, key):
            if key in db_store:
                del db_store[key]
                return 1
            return 0

    monkeypatch.setattr("core.voice.ivr_router.get_redis", lambda: MockRedis())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Test Inbound Call greeting TwiML XML
        res = await client.post(
            "/admin/voice/inbound",
            data={"CallSid": "call_456", "From": "+18005550199"},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        assert "application/xml" in res.headers["content-type"]
        assert "<Response>" in res.text
        assert "Gather" in res.text

        # 2. Test Gather options keypress routing TwiML XML
        res_gather = await client.post(
            "/admin/voice/inbound/gather",
            data={"CallSid": "call_456", "Digits": "1"},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_gather.status_code == 200
        assert "application/xml" in res_gather.headers["content-type"]
        assert "Returns and Exchanges" in res_gather.text

        # 3. Test GET active list
        res_list = await client.get(
            "/admin/voice/inbound/active",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_list.status_code == 200
        assert len(res_list.json()["items"]) == 1

        # 4. Test simulate hangup endpoint
        res_hang = await client.post(
            "/admin/voice/inbound/hangup?call_sid=call_456",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_hang.status_code == 200
        assert res_hang.json()["status"] == "success"
