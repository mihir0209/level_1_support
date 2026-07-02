"""Tests for KB Policy Conflict Verification features."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from main import app
from core.knowledge.verifier import verify_policy_conflicts
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_verify_policy_conflicts(monkeypatch):
    """Verify that verify_policy_conflicts correctly catches contradictions via LLM client."""
    mock_llm = MagicMock()
    # Mock contradiction report response from LLM
    mock_llm.generate = AsyncMock(
        return_value="""
        [
          {
            "conflict_type": "contradiction",
            "existing_rule": "Returns are permitted within 28 days of order delivery for standard items.",
            "proposed_rule": "Standard items must be returned within 14 days of delivery.",
            "explanation": "The proposed rule reduces the return window to 14 days, contradicting the existing 28 days."
          }
        ]
        """
    )
    monkeypatch.setattr("core.knowledge.verifier.get_llm_client", lambda: mock_llm)

    conflicts = await verify_policy_conflicts(
        brand_id="boohoo_uk",
        new_policy_text="Standard items must be returned within 14 days of delivery."
    )
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "contradiction"
    assert "contradicting the existing 28 days" in conflicts[0]["explanation"]


@pytest.mark.asyncio
async def test_kb_conflict_endpoints(monkeypatch):
    """Verify KB conflict API endpoints store and resolve contradicts."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        return_value="""
        [
          {
            "conflict_type": "contradiction",
            "existing_rule": "Returns are permitted within 28 days.",
            "proposed_rule": "Returns within 10 days.",
            "explanation": "Contradiction."
          }
        ]
        """
    )
    monkeypatch.setattr("core.knowledge.verifier.get_llm_client", lambda: mock_llm)

    # Mock Redis data store
    db_store = {}

    class MockRedis:
        async def hset(self, key, field, val):
            db_store[field] = val
            return 1
        async def hgetall(self, key):
            return db_store
        async def hdel(self, key, field):
            db_store.pop(field, None)
            return 1

    monkeypatch.setattr("core.memory.session.get_redis", lambda: MockRedis())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Run Analysis
        res_analyze = await client.post(
            "/admin/kb/analyze-policy",
            json={
                "brand_id": "boohoo_uk",
                "text": "Returns within 10 days."
            },
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_analyze.status_code == 200
        data_analyze = res_analyze.json()
        assert len(data_analyze["conflicts"]) == 1
        assert len(db_store) == 1

        # 2. Get list of conflicts
        res_get = await client.get(
            "/admin/kb/conflicts",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_get.status_code == 200
        data_get = res_get.json()
        assert data_get["count"] == 1
        conflict_id = data_get["items"][0]["id"]

        # 3. Resolve conflict
        res_resolve = await client.post(
            "/admin/kb/resolve-conflict",
            json={"conflict_id": conflict_id},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_resolve.status_code == 200
        assert res_resolve.json()["resolved_id"] == conflict_id
        assert len(db_store) == 0
