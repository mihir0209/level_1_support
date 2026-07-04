"""Tests for Prompt strategy A/B simulator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from main import app
from core.orchestration.simulator import (
    get_simulation_results,
    run_batch_simulation,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_simulator_batch_runner(monkeypatch):
    """Verify that run_batch_simulation records and stores simulator trials inside Redis."""
    db_store = []

    class MockRedis:
        async def lpush(self, key, val):
            db_store.append(val)
            return len(db_store)
        async def ltrim(self, key, start, stop):
            return True
        async def lrange(self, key, start, stop):
            return db_store

    monkeypatch.setattr("core.orchestration.simulator.get_redis", lambda: MockRedis())

    # Run
    res = await run_batch_simulation(
        empathy_mode="friendly",
        num_runs=5,
        persona_mix={"cooperative": 1.0, "frustrated": 0.0, "confused": 0.0}
    )

    assert res["empathy_mode"] == "friendly"
    assert res["num_runs"] == 5
    assert len(db_store) == 1

    # Fetch
    history = await get_simulation_results()
    assert len(history) == 1
    assert history[0]["empathy_mode"] == "friendly"


@pytest.mark.asyncio
async def test_simulator_api_endpoints(monkeypatch):
    """Verify REST interfaces executing simulations and fetching metrics history."""
    db_store = [
        '{"empathy_mode": "friendly", "num_runs": 10, "persona_mix": {"cooperative": 1.0}, "resolution_rate": 0.9, "avg_turns": 2.0, "avg_compliance": 1.0, "avg_risk": 0.05, "timestamp": 1783012345}'
    ]

    class MockRedis:
        async def lpush(self, key, val):
            db_store.append(val)
            return len(db_store)
        async def ltrim(self, key, start, stop):
            return True
        async def lrange(self, key, start, stop):
            return db_store

    monkeypatch.setattr("core.orchestration.simulator.get_redis", lambda: MockRedis())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET Results
        res = await client.get(
            "/admin/simulator/results",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 1
        assert data["items"][0]["empathy_mode"] == "friendly"

        # 2. POST Run
        res_run = await client.post(
            "/admin/simulator/run",
            json={
                "empathy_mode": "concise",
                "num_runs": 5,
                "persona_mix": {"cooperative": 0.5, "frustrated": 0.5, "confused": 0.0}
            },
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_run.status_code == 200
        assert res_run.json()["empathy_mode"] == "concise"
        assert len(db_store) == 2
