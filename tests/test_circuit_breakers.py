"""Tests for Tool API Circuit Breakers and Fallback Routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from main import app
from core.tools.circuit_breaker import (
    track_tool_failure,
    track_tool_success,
    is_tool_tripped,
    reset_tool_breaker,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_tool_circuit_breaker_logic(monkeypatch):
    """Verify that circuit breaker status transitions correctly based on failure/success counts."""
    # Mock Redis data store
    db_store = {}

    class MockRedis:
        async def incr(self, key):
            val = db_store.get(key, 0) + 1
            db_store[key] = val
            return val
        async def set(self, key, val):
            db_store[key] = val
            return True
        async def setex(self, key, expire, val):
            db_store[key] = val
            return True
        async def get(self, key):
            return db_store.get(key)
        async def delete(self, *keys):
            for k in keys:
                db_store.pop(k, None)
            return True

    monkeypatch.setattr("core.tools.circuit_breaker.get_redis", lambda: MockRedis())

    # Initial state should be active (not tripped)
    assert not await is_tool_tripped("order_lookup")

    # 1. First failure
    tripped = await track_tool_failure("order_lookup")
    assert not tripped
    assert not await is_tool_tripped("order_lookup")

    # 2. Second failure
    tripped = await track_tool_failure("order_lookup")
    assert not tripped
    assert not await is_tool_tripped("order_lookup")

    # 3. Third failure -> should trip!
    tripped = await track_tool_failure("order_lookup")
    assert tripped
    assert await is_tool_tripped("order_lookup")

    # 4. Track success -> should reset error count
    await track_tool_success("order_lookup")
    assert db_store.get("circuit_breaker:order_lookup:failures") == 0

    # 5. Manual Reset -> clears tripped status
    await reset_tool_breaker("order_lookup")
    assert not await is_tool_tripped("order_lookup")


@pytest.mark.asyncio
async def test_circuit_breaker_endpoints(monkeypatch):
    """Verify admin endpoints get breaker lists and reset statuses."""
    db_store = {
        "circuit_breaker:order_lookup:status": "tripped",
        "circuit_breaker:order_lookup:failures": "3"
    }

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def delete(self, *keys):
            for k in keys:
                db_store.pop(k, None)
            return True

    monkeypatch.setattr("core.tools.circuit_breaker.get_redis", lambda: MockRedis())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Fetch breakers status
        res_get = await client.get(
            "/admin/tools/circuit-breakers",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_get.status_code == 200
        data_get = res_get.json()
        
        lookup_breaker = next(b for b in data_get["breakers"] if b["tool_name"] == "order_lookup")
        assert lookup_breaker["status"] == "tripped"
        assert lookup_breaker["failures"] == 3

        # 2. Reset breaker
        res_reset = await client.post(
            "/admin/tools/circuit-breakers/reset",
            json={"tool_name": "order_lookup"},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_reset.status_code == 200
        assert res_reset.json()["status"] == "success"
        assert "circuit_breaker:order_lookup:status" not in db_store
