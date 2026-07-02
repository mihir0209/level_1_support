"""Tests for In-Session Reinforcement Learning & Live RL Policy Selector."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from main import app
from core.orchestration.rl_engine import (
    select_best_arm,
    register_feedback,
    get_all_arm_stats,
    reset_arm_stats,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_thompson_sampling_logic(monkeypatch):
    """Verify Thompson Sampling arm selection and Beta feedback updates."""
    db_store = {}

    class MockRedis:
        async def exists(self, key):
            return key in db_store
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True
        async def incrbyfloat(self, key, amount):
            val = float(db_store.get(key, 0.0)) + amount
            db_store[key] = str(val)
            return val
        async def delete(self, *keys):
            for k in keys:
                db_store.pop(k, None)
            return True

    monkeypatch.setattr("core.orchestration.rl_engine.get_redis", lambda: MockRedis())

    # 1. Selection with uniform Beta(1,1) priors
    arm = await select_best_arm("empathy_mode")
    assert arm in ["professional", "friendly", "concise"]

    # 2. Register positive feedback for concise -> Alpha becomes 2.0
    await register_feedback("empathy_mode", "concise", reward=1)
    assert db_store["rl:empathy_mode:concise:alpha"] == "2.0"

    # 3. Register negative feedback for professional -> Beta becomes 2.0
    await register_feedback("empathy_mode", "professional", reward=0)
    assert db_store["rl:empathy_mode:professional:beta"] == "2.0"

    # 4. Get stats
    stats = await get_all_arm_stats("empathy_mode")
    assert len(stats) == 3
    concise_stat = next(s for s in stats if s["arm_name"] == "concise")
    assert concise_stat["alpha"] == 2.0
    assert concise_stat["beta"] == 1.0
    # Expected win rate of concise is 2 / (2 + 1) = 0.667
    assert concise_stat["expected_win_rate"] == 0.667

    # 5. Reset stats
    await reset_arm_stats("empathy_mode")
    assert len(db_store) == 0


@pytest.mark.asyncio
async def test_takeover_rl_feedback(monkeypatch):
    """Verify that operator takeover/intercept triggers negative feedback on the empathy arm."""
    db_store = {}

    class MockRedis:
        async def exists(self, key):
            return key in db_store
        async def set(self, key, val):
            db_store[key] = val
            return True
        async def get(self, key):
            return db_store.get(key)
        async def incrbyfloat(self, key, amount):
            val = float(db_store.get(key, 0.0)) + amount
            db_store[key] = str(val)
            return val

    monkeypatch.setattr("core.orchestration.rl_engine.get_redis", lambda: MockRedis())

    # Mock active session details
    mock_session = {
        "session_id": "rl_takeover_test_session",
        "brand_id": "boohoo_uk",
        "channel": "chat",
        "turn_count": 1,
        "empathy_mode": "friendly",
        "messages": [],
    }
    async def mock_get_session(sid):
        return mock_session
    async def mock_update_session(sid, data):
        mock_session.update(data)
        return True

    monkeypatch.setattr("main.get_session", mock_get_session)
    monkeypatch.setattr("main.update_session", mock_update_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Trigger takeover override
        res = await client.post(
            "/admin/session/rl_takeover_test_session/takeover",
            json={"is_takeover": True},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        assert res.json()["status"] == "success"

        # Verify negative feedback was registered for 'friendly'
        assert db_store["rl:empathy_mode:friendly:beta"] == "2.0"
        assert mock_session["rl_feedback_registered"] is True
