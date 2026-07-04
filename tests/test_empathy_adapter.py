"""Tests for Sentiment-Based Dynamic Empathy Adapters."""

from __future__ import annotations

import pytest
from main import app
from core.orchestration.empathy_adapter import (
    get_empathy_rules,
    save_empathy_rules,
    adjust_empathy_mode,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_empathy_rules_persistence(monkeypatch):
    """Verify threshold override rule gets and sets persist in Redis correctly."""
    db_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.memory.session.get_redis", lambda: MockRedis())

    custom_rules = {
        "frustrated_threshold": 0.40,
        "frustrated_mode": "concise",
        "confused_threshold": 0.60,
        "confused_mode": "friendly",
        "override_activations": 1
    }
    await save_empathy_rules(custom_rules)
    assert "empathy:rules:config" in db_store

    rules = await get_empathy_rules()
    assert rules["frustrated_threshold"] == 0.40
    assert rules["frustrated_mode"] == "concise"


@pytest.mark.asyncio
async def test_adjust_empathy_mode_override(monkeypatch):
    """Verify that mode updates to target override if sentiment hits thresholds."""
    db_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.memory.session.get_redis", lambda: MockRedis())

    # Case A: High sentiment (neutral/happy 0.8) -> no change
    mode_ok = await adjust_empathy_mode("s_emp_1", 0.80, "professional")
    assert mode_ok == "professional"

    # Case B: Very low sentiment (frustrated 0.2) -> override to friendly
    mode_low = await adjust_empathy_mode("s_emp_2", 0.20, "professional")
    assert mode_low == "friendly"


@pytest.mark.asyncio
async def test_empathy_adapter_api_endpoints(monkeypatch):
    """Verify API endpoints can query and save override rules maps."""
    db_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.memory.session.get_redis", lambda: MockRedis())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET default rules
        res = await client.get(
            "/admin/empathy/rules",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        assert res.json()["frustrated_mode"] == "friendly"

        # POST update rules
        res_post = await client.post(
            "/admin/empathy/rules",
            json={
                "frustrated_threshold": 0.25,
                "frustrated_mode": "concise",
                "confused_threshold": 0.50,
                "confused_mode": "professional"
            },
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_post.status_code == 200

        # Verify saved state
        res_after = await client.get(
            "/admin/empathy/rules",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_after.json()["frustrated_threshold"] == 0.25
        assert res_after.json()["frustrated_mode"] == "concise"
