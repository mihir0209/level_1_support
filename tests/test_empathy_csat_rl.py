"""Tests for Auto-Tuned Empathy Persona Selector via CSAT Multi-Armed Bandit (Phase 31)."""

from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient
from main import app
from core.orchestration.rl_engine import register_feedback, get_all_arm_stats


@pytest.mark.asyncio
async def test_rl_engine_continuous_reward_update(monkeypatch):
    """Verify that continuous rewards update Alpha and Beta distributions correctly."""
    redis_mock_db = {}

    class FakeRedis:
        async def exists(self, key):
            return key in redis_mock_db
        async def set(self, key, value):
            redis_mock_db[key] = float(value)
        async def get(self, key):
            return str(redis_mock_db.get(key, 1.0)).encode()
        async def incrbyfloat(self, key, value):
            current = redis_mock_db.get(key, 1.0)
            redis_mock_db[key] = current + float(value)

    monkeypatch.setattr("core.orchestration.rl_engine.get_redis", FakeRedis)

    # 1. Register continuous reward of 0.75
    # Alpha should increment by 0.75, Beta should increment by 0.25 (1.0 - 0.75)
    await register_feedback("empathy_mode", "friendly", 0.75)
    
    alpha = redis_mock_db["rl:empathy_mode:friendly:alpha"]
    beta = redis_mock_db["rl:empathy_mode:friendly:beta"]
    
    # 1.0 (prior baseline) + 0.75 = 1.75
    assert alpha == 1.75
    # 1.0 (prior baseline) + 0.25 = 1.25
    assert beta == 1.25

    # 2. Verify Expected Win Rate calculation
    # expected = alpha / (alpha + beta) = 1.75 / (1.75 + 1.25) = 1.75 / 3.0 = 0.583
    stats = await get_all_arm_stats("empathy_mode")
    friendly_stat = next(s for s in stats if s["arm_name"] == "friendly")
    assert friendly_stat["expected_win_rate"] == 0.583


@pytest.mark.asyncio
async def test_csat_response_rl_integration(monkeypatch):
    """Verify that completing a CSAT survey registers normalized rewards to the RL engine."""
    registered_rewards = []

    async def mock_register_feedback(category, arm_name, reward):
        registered_rewards.append((category, arm_name, reward))

    monkeypatch.setattr("core.orchestration.rl_engine.register_feedback", mock_register_feedback)
    
    # Mock Redis session fetch in csat.py completion flow
    async def mock_get_session(sid):
        return {
            "session_id": sid,
            "empathy_mode": "friendly",
            "chatwoot_conv_id": "conv_123"
        }
    monkeypatch.setattr("core.crm.csat.get_session", mock_get_session)
    monkeypatch.setattr("core.crm.csat.update_session", lambda *a, **k: None)
    monkeypatch.setattr("core.crm.csat.invalidate_customer_cache", lambda *a, **k: None)

    # Mock database connections
    class MockConn:
        async def execute(self, *a, **k):
            pass
    class MockPool:
        def connection(self):
            class AsyncContext:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return AsyncContext()
    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: MockPool())

    class FakeRedis:
        async def get(self, key):
            return b"cust_1"
        async def delete(self, key):
            pass
    monkeypatch.setattr("core.crm.csat.get_redis", FakeRedis)
    
    # Mock Chatwoot message post
    monkeypatch.setattr("channels.chat.chatwoot_client.post_message", lambda *a, **k: True)

    from core.crm.csat import process_csat_response
    
    # A CSAT score of 5 should normalize to (5.0 - 1.0) / 4.0 = 1.0 reward
    success = await process_csat_response("session_abc", "My score is 5")
    assert success is True
    
    assert len(registered_rewards) == 1
    assert registered_rewards[0] == ("empathy_mode", "friendly", 1.0)

    # A CSAT score of 3 should normalize to (3.0 - 1.0) / 4.0 = 0.5 reward
    registered_rewards.clear()
    await process_csat_response("session_abc", "I rate it a 3")
    assert registered_rewards[0] == ("empathy_mode", "friendly", 0.5)


@pytest.mark.asyncio
async def test_rl_stats_endpoint_includes_csat(monkeypatch):
    """Verify that GET /admin/rl/stats endpoint merges Postgres CSAT averages."""
    monkeypatch.setattr("config.settings.postgres_host", "localhost")

    # Mock get_all_arm_stats
    async def mock_get_all_arm_stats(category):
        return [
            {"arm_name": "professional", "alpha": 2.0, "beta": 1.0, "trials": 1, "expected_win_rate": 0.67},
            {"arm_name": "friendly", "alpha": 1.0, "beta": 1.0, "trials": 0, "expected_win_rate": 0.50}
        ]
    monkeypatch.setattr("core.orchestration.rl_engine.get_all_arm_stats", mock_get_all_arm_stats)

    # Mock database to return average satisfaction
    class MockConn:
        async def execute(self, query, params=None):
            class MockResult:
                async def fetchone(self):
                    # If query is checking professional, return average 4.5
                    if params and params[0] == "professional":
                        return (4.5,)
                    return (3.2,)
            return MockResult()

    class MockPool:
        def connection(self):
            class AsyncContext:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return AsyncContext()

    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: MockPool())

    client = TestClient(app)
    response = client.get("/admin/rl/stats?category=empathy_mode", headers={"X-Admin-Key": "l1support-demo-key-2026"})
    assert response.status_code == 200
    
    data = response.json()
    assert "stats" in data
    
    prof_stat = next(s for s in data["stats"] if s["arm_name"] == "professional")
    friendly_stat = next(s for s in data["stats"] if s["arm_name"] == "friendly")
    
    assert prof_stat["avg_csat"] == 4.5
    assert friendly_stat["avg_csat"] == 3.2
