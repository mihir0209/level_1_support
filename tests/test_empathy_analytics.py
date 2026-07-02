"""Tests for Empathy Persona Tuning and Knowledge Graph Analytics."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from core.ontology.models import Channel, IntentResult, SessionOutcome
from core.orchestration.graph import _build_llm_messages
from core.orchestration.state import L1SupportState
from main import app, analyze_sentiment
from httpx import ASGITransport, AsyncClient


def test_sentiment_analyzer():
    """Verify sentiment categorizer flags frustrated, happy, or neutral messages."""
    assert analyze_sentiment("I hate this service, it is terrible and broken") == "frustrated"
    assert analyze_sentiment("Thank you so much! Excellent help and solved my issue") == "happy"
    assert analyze_sentiment("Where is my order package status?") == "neutral"


@pytest.mark.asyncio
async def test_empathy_system_prompt_directives(monkeypatch):
    """Verify system prompt generation dynamically appends empathy persona guidelines."""
    # Instantiated context package
    from core.ontology.models import ContextPackage
    mock_ctx = ContextPackage(
        brand_id="boohoo_uk",
        customer_id="cust_123",
        channel=Channel.CHAT,
        system_prompt="Global system prompt.",
        user_context_block="User profile context.",
        policy_block="Policy return rules.",
        shipping_block="Shipping window details.",
        resolution_block="Resolution path instructions.",
        session_block="Active conversation context.",
        tool_manifest=[],
    )

    # Test Apologetic
    state_apologetic = L1SupportState(
        session_id="test_session_empathy",
        brand_id="boohoo_uk",
        channel=Channel.CHAT,
        context_package=mock_ctx,
        empathy_mode="apologetic",
    )
    
    # Mock LLM translate
    mock_llm = MagicMock()
    mock_llm.translate_content = AsyncMock(side_effect=lambda text, lang: text)
    monkeypatch.setattr("core.orchestration.graph.get_llm_client", lambda: mock_llm)

    msgs_apologetic = await _build_llm_messages(state_apologetic)
    system_prompt_apologetic = msgs_apologetic[0].content
    assert "AGENT PERSONA (APOLOGETIC)" in system_prompt_apologetic
    assert "apologize for any customer inconvenience" in system_prompt_apologetic

    # Test Concise
    state_concise = L1SupportState(
        session_id="test_session_empathy",
        brand_id="boohoo_uk",
        channel=Channel.CHAT,
        context_package=mock_ctx,
        empathy_mode="concise",
    )
    msgs_concise = await _build_llm_messages(state_concise)
    system_prompt_concise = msgs_concise[0].content
    assert "AGENT PERSONA (CONCISE)" in system_prompt_concise
    assert "Direct and minimalist" in system_prompt_concise


@pytest.mark.asyncio
async def test_intent_performance_analytics_endpoint(monkeypatch):
    """Verify that intent performance returns aggregated data from DB and FalkorDB."""
    dummy_intent = {
        "intent": "returns",
        "avg_csat": 0.85,
        "total_sessions": 10,
        "resolved_count": 8,
        "escalated_count": 2,
        "escalation_rate": 20.0
    }
    dummy_resolution = {
        "name": "return_init",
        "success_rate": 0.90
    }

    # Mock DB connection row return
    class MockCursor:
        def execute(self, query, params=None):
            pass
        def fetchall(self):
            return [("returns", 0.85, 10, 8, 2)]
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockConn:
        def cursor(self):
            return MockCursor()
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
        def close(self):
            pass

    monkeypatch.setattr("feedback.engine._get_db_conn", lambda: MockConn())

    # Mock FalkorDB query
    mock_graph = MagicMock()
    mock_graph.query = MagicMock(
        return_value=MagicMock(result_set=[["return_init", 0.90]])
    )
    monkeypatch.setattr("main.get_graph_client", lambda: ("client", mock_graph))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/admin/analytics/intent-performance", headers={"X-Admin-Key": "l1support-demo-key-2026"})
        assert res.status_code == 200
        data = res.json()
        assert "postgres_intents" in data
        assert "kg_resolutions" in data
        assert data["postgres_intents"][0]["intent"] == "returns"
        assert data["postgres_intents"][0]["escalation_rate"] == 20.0
        assert data["kg_resolutions"][0]["name"] == "return_init"
        assert data["kg_resolutions"][0]["success_rate"] == 0.90
