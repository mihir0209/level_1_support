"""Tests for Customer History Enrichment."""

from __future__ import annotations

import json
import pytest
from core.crm.customer_history import (
    get_customer_history,
    format_history_block,
)
from core.context.engine import assemble_context
from core.ontology.models import Channel, Customer, CustomerTier


def test_format_history_block_empty():
    """Verify that formatting empty history returns an empty string."""
    assert format_history_block([]) == ""


def test_format_history_block_standard():
    """Verify formatting standard customer history."""
    history = [
        {
            "session_id": "sess_1",
            "brand_id": "boohoo_uk",
            "intent": "order_tracking",
            "outcome": "resolved",
            "satisfaction": 4.0,
            "started_at": "2026-07-01T12:00:00Z",
            "ended_at": "2026-07-01T12:05:00Z",
            "turn_count": 2,
        }
    ]
    block = format_history_block(history)
    assert "## Returning Customer Context" in block
    assert "2026-07-01: Intent=order_tracking, Outcome=resolved, Satisfaction=4.0/5" in block
    assert "⚠️" not in block


def test_format_history_block_negative():
    """Verify warning instructions are included when history has escalations or low satisfaction."""
    # Low satisfaction (satisfaction < 3.0)
    history_low_sat = [
        {
            "session_id": "sess_2",
            "brand_id": "boohoo_uk",
            "intent": "return_request",
            "outcome": "resolved",
            "satisfaction": 2.0,
            "started_at": "2026-07-02T12:00:00Z",
            "ended_at": "2026-07-02T12:15:00Z",
            "turn_count": 5,
        }
    ]
    block_low_sat = format_history_block(history_low_sat)
    assert "⚠️ This customer has had negative past experiences." in block_low_sat

    # Escalated outcome
    history_escalated = [
        {
            "session_id": "sess_3",
            "brand_id": "boohoo_uk",
            "intent": "refund_inquiry",
            "outcome": "escalated",
            "satisfaction": None,
            "started_at": "2026-07-03T12:00:00Z",
            "ended_at": "2026-07-03T12:15:00Z",
            "turn_count": 4,
        }
    ]
    block_escalated = format_history_block(history_escalated)
    assert "⚠️ This customer has had negative past experiences." in block_escalated


@pytest.mark.asyncio
async def test_get_customer_history_cache_hit(monkeypatch):
    """Verify cache hit path avoids DB query."""
    db_called = False

    class MockRedis:
        async def get(self, key):
            assert "customer_history:cust_123" in key
            return json.dumps([
                {
                    "session_id": "sess_cached",
                    "brand_id": "boohoo_uk",
                    "intent": "cached_intent",
                    "outcome": "resolved",
                    "satisfaction": 5.0,
                    "started_at": "2026-07-04T12:00:00",
                    "turn_count": 1,
                }
            ])

        async def setex(self, key, ttl, val):
            pass

    monkeypatch.setattr("core.crm.customer_history.get_redis", lambda: MockRedis())

    async def mock_db_fetch(customer_id, limit):
        nonlocal db_called
        db_called = True
        return []

    monkeypatch.setattr("core.crm.customer_history._fetch_history_from_db", mock_db_fetch)

    history = await get_customer_history("cust_123")
    assert len(history) == 1
    assert history[0]["session_id"] == "sess_cached"
    assert not db_called


@pytest.mark.asyncio
async def test_get_customer_history_cache_miss(monkeypatch):
    """Verify cache miss queries DB and populates cache."""
    cache_store = {}

    class MockRedis:
        async def get(self, key):
            return cache_store.get(key)

        async def setex(self, key, ttl, val):
            cache_store[key] = val
            return True

    monkeypatch.setattr("core.crm.customer_history.get_redis", lambda: MockRedis())

    async def mock_db_fetch(customer_id, limit):
        return [
            {
                "session_id": "sess_db",
                "brand_id": "boohoo_uk",
                "intent": "db_intent",
                "outcome": "resolved",
                "satisfaction": 4.5,
                "started_at": "2026-07-04T12:00:00",
                "turn_count": 3,
            }
        ]

    monkeypatch.setattr("core.crm.customer_history._fetch_history_from_db", mock_db_fetch)

    history = await get_customer_history("cust_456")
    assert len(history) == 1
    assert history[0]["session_id"] == "sess_db"
    assert f"customer_history:cust_456" in cache_store


@pytest.mark.asyncio
async def test_assemble_context_integration(monkeypatch):
    """Verify context assembly retrieves and appends customer history to user_context_block."""
    # Mock LLM intent classification
    from core.ontology.models import IntentResult, SupportCategory, ExtractedEntities
    class MockLLM:
        async def chat(self, *args, **kwargs):
            return ""

    async def mock_classify(*args, **kwargs):
        return IntentResult(
            issue_type="order_tracking",
            confidence=0.95,
            category=SupportCategory.SERVICE_STATUS,
            entities=ExtractedEntities()
        )
    monkeypatch.setattr("core.context.engine.classify_intent", mock_classify)

    # Mock Graphiti search to prevent actual client queries
    async def mock_graphiti_search(*args, **kwargs):
        return ["Fact 1", "Fact 2"]
    monkeypatch.setattr("core.memory.graphiti_client.graphiti_memory.search", mock_graphiti_search)

    # Mock customer history fetch to return sample history
    async def mock_get_history(customer_id, limit=5):
        return [
            {
                "session_id": "sess_prev",
                "brand_id": "boohoo_uk",
                "intent": "order_tracking",
                "outcome": "escalated",
                "satisfaction": 1.0,
                "started_at": "2026-07-01T12:00:00Z",
                "turn_count": 5,
            }
        ]
    monkeypatch.setattr("core.crm.customer_history.get_customer_history", mock_get_history)

    # Mock get_brand_info / retrieve_knowledge
    from unittest.mock import MagicMock
    monkeypatch.setattr("core.context.engine.get_brand_info", lambda brand_id: {"name": "Boohoo"})

    class MockKnowledge:
        policy = None
        shipping_policy = None
        resolutions = []
        kb_items = []
        source = "test"
        confidence = 1.0
    monkeypatch.setattr("core.context.engine.retrieve_knowledge", lambda *a: MockKnowledge())

    customer = Customer(id="cust_test", brand_id="boohoo_uk", email="test@test.com", tier=CustomerTier.VIP)

    ctx_pkg = await assemble_context(
        message="Where is my order?",
        session_id="sess_current",
        brand_id="boohoo_uk",
        channel=Channel.CHAT,
        conversation_history=[],
        customer=customer,
        llm_client=MockLLM(),
    )

    assert ctx_pkg.user_context_block is not None
    assert "## Returning Customer Context" in ctx_pkg.user_context_block
    assert "Intent=order_tracking, Outcome=escalated, Satisfaction=1.0/5" in ctx_pkg.user_context_block
    assert "⚠️ This customer has had negative past experiences." in ctx_pkg.user_context_block


def test_admin_customer_history_api(monkeypatch):
    """Verify that the GET /admin/customers/{customer_id}/history endpoint behaves correctly."""
    from fastapi.testclient import TestClient
    from main import app

    async def mock_get_history(customer_id, limit=5):
        return [
            {
                "session_id": "sess_123",
                "brand_id": "boohoo_uk",
                "intent": "refund",
                "outcome": "resolved",
                "satisfaction": 5.0,
                "started_at": "2026-07-04T12:00:00Z",
                "turn_count": 2,
            }
        ]

    monkeypatch.setattr("core.crm.customer_history.get_customer_history", mock_get_history)

    client = TestClient(app)
    response = client.get("/admin/customers/cust_test_123/history")
    assert response.status_code == 200
    data = response.json()
    assert data["customer_id"] == "cust_test_123"
    assert data["session_count"] == 1
    assert data["sessions"][0]["session_id"] == "sess_123"
    assert "## Returning Customer Context" in data["prompt_block"]

