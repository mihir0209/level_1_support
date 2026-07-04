"""Tests for Multi-Model Fallback & Cost Optimization Coordinator."""

from __future__ import annotations

import pytest
from main import app
from core.orchestration.fallback_coordinator import (
    get_fallback_config,
    save_fallback_config,
    call_llm_with_fallback,
    get_gateway_analytics,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_fallback_config_store(monkeypatch):
    """Verify set and get gateway configurations persistence inside Redis."""
    db_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.orchestration.fallback_coordinator.get_redis", lambda: MockRedis())

    # Save custom config
    custom = {
        "primary_model": "ollama/llama3",
        "fallback_model": "openai/gpt-4o",
        "rate_limit_mitigations": 2,
        "pricing": {}
    }
    await save_fallback_config(custom)
    assert "llm:fallback:config" in db_store

    # Read back
    config = await get_fallback_config()
    assert config["primary_model"] == "ollama/llama3"
    assert config["rate_limit_mitigations"] == 2


@pytest.mark.asyncio
async def test_fallback_triggers_and_costing(monkeypatch):
    """Verify model swap triggers on failure, costing updates and savings logs."""
    db_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True
        async def incrbyfloat(self, key, amount):
            val = float(db_store.get(key) or "0.0")
            db_store[key] = str(val + amount)
            return val + amount

    monkeypatch.setattr("core.orchestration.fallback_coordinator.get_redis", lambda: MockRedis())

    # 1. Run compliant case without failures
    res_ok = await call_llm_with_fallback(
        messages=[{"role": "user", "content": "How to return?"}],
        session_id="s_fallback_ok"
    )
    assert res_ok["model"] == "openai/gpt-4o"
    assert res_ok["fallback_triggered"] is False
    assert float(db_store["session:s_fallback_ok:llm_cost"]) > 0.0

    # 2. Run fallback failover case
    res_fail = await call_llm_with_fallback(
        messages=[{"role": "user", "content": "Hello, delay package?"}],
        session_id="s_fallback_fail",
        force_fail_primary=True
    )
    assert res_fail["model"] == "anthropic/claude-3-5-sonnet"
    assert res_fail["fallback_triggered"] is True
    assert float(db_store["llm:cumulative_savings"]) >= 0.0


@pytest.mark.asyncio
async def test_fallback_api_routes(monkeypatch):
    """Verify admin config routing getters and setters return correctly."""
    db_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.orchestration.fallback_coordinator.get_redis", lambda: MockRedis())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET default fallback
        res = await client.get(
            "/admin/llm/fallback",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        assert res.json()["primary_model"] == "openai/gpt-4o"

        # POST update fallback preferences
        res_post = await client.post(
            "/admin/llm/fallback",
            json={
                "primary_model": "ollama/llama3",
                "fallback_model": "anthropic/claude-3-5-sonnet"
            },
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_post.status_code == 200
        
        # Verify saved state
        res_after = await client.get(
            "/admin/llm/fallback",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_after.json()["primary_model"] == "ollama/llama3"
