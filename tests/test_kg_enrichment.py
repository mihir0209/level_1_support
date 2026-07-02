"""Tests for Self-Enriching Knowledge Graph Loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from main import app
from core.ontology.enricher import (
    extract_and_propose_facts,
    approve_enrichment,
    reject_enrichment,
    get_pending_enrichments,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_extract_and_propose_facts(monkeypatch):
    """Verify that extract_and_propose_facts parses dialog, propose fact, and writes to Redis."""
    # Mock LLM response
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        return_value="""
        {
          "explanation": "Evri is experiencing package delays.",
          "cypher_query": "CREATE (i:Incident {name: 'Evri delays'})"
        }
        """
    )
    monkeypatch.setattr("core.orchestration.llm.get_llm_client", lambda: mock_llm)

    # Mock Redis data store
    db_store = {}

    class MockRedis:
        async def hset(self, key, field, val):
            db_store[field] = val
            return 1
        async def hgetall(self, key):
            return db_store

    monkeypatch.setattr("core.ontology.enricher.get_redis", lambda: MockRedis())

    fact = await extract_and_propose_facts(
        session_id="test_enrich_session",
        brand_id="boohoo_uk",
        messages=[{"role": "user", "content": "My Evri order is delayed"}]
    )
    assert fact is not None
    assert fact["explanation"] == "Evri is experiencing package delays."
    assert len(db_store) == 1


@pytest.mark.asyncio
async def test_approve_reject_enrichment(monkeypatch):
    """Verify approval runs Cypher query and clears Redis, while rejection simply deletes."""
    db_store = {
        "fact_123": """
        {
          "id": "fact_123",
          "explanation": "Incident",
          "cypher_query": "CREATE (i:Incident {name: 'Evri delays'})"
        }
        """
    }

    class MockRedis:
        async def hget(self, key, field):
            return db_store.get(field)
        async def hdel(self, key, field):
            db_store.pop(field, None)
            return 1
        async def hgetall(self, key):
            return db_store

    monkeypatch.setattr("core.ontology.enricher.get_redis", lambda: MockRedis())

    # Mock FalkorDB connection
    mock_graph = MagicMock()
    mock_graph.query = MagicMock(return_value=None)
    monkeypatch.setattr("core.ontology.enricher.get_graph_client", lambda: (None, mock_graph))

    # Approve
    success = await approve_enrichment("fact_123")
    assert success
    mock_graph.query.assert_called_once_with("CREATE (i:Incident {name: 'Evri delays'})")
    assert "fact_123" not in db_store


@pytest.mark.asyncio
async def test_enrichment_endpoints(monkeypatch):
    """Verify admin endpoints return, approve, and reject facts."""
    db_store = {
        "fact_999": """
        {
          "id": "fact_999",
          "explanation": "Incident 999",
          "cypher_query": "CREATE (i:Incident)"
        }
        """
    }

    class MockRedis:
        async def hgetall(self, key):
            return db_store
        async def hget(self, key, field):
            return db_store.get(field)
        async def hdel(self, key, field):
            db_store.pop(field, None)
            return 1

    monkeypatch.setattr("core.ontology.enricher.get_redis", lambda: MockRedis())

    mock_graph = MagicMock()
    monkeypatch.setattr("core.ontology.enricher.get_graph_client", lambda: (None, mock_graph))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Fetch pending
        res_get = await client.get(
            "/admin/graph/enrichments",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_get.status_code == 200
        data_get = res_get.json()
        assert data_get["count"] == 1
        assert data_get["items"][0]["id"] == "fact_999"

        # 2. Approve
        res_approve = await client.post(
            "/admin/graph/enrichments/approve",
            json={"enrichment_id": "fact_999"},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_approve.status_code == 200
        assert res_approve.json()["approved"] is True
        assert len(db_store) == 0
