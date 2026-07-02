"""Tests for the Operator Dashboard approvals API and metrics tracking."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from core.metrics import APPROVAL_DECISIONS_TOTAL, APPROVAL_LATENCY_SECONDS, APPROVAL_REQUESTS_TOTAL
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from main import app


@pytest.fixture(autouse=True)
def clean_metrics():
    """Reset approval metrics before each test."""
    APPROVAL_REQUESTS_TOTAL.clear()
    APPROVAL_DECISIONS_TOTAL.clear()
    APPROVAL_LATENCY_SECONDS.clear()


@pytest.mark.asyncio
async def test_get_pending_approvals_empty(monkeypatch):
    """Verify pending approvals returns empty list when no sessions are paused."""
    async def mock_get_paused():
        return []

    monkeypatch.setattr("main.get_all_paused_sessions", mock_get_paused)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/admin/pending-approvals", headers={"X-Admin-Key": "l1support-demo-key-2026"})
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 0
        assert data["items"] == []


@pytest.mark.asyncio
async def test_get_pending_approvals_with_items(monkeypatch):
    """Verify pending approvals returns items when sessions are paused."""
    dummy_item = {
        "session_id": "session_abc",
        "brand_id": "boohoo_uk",
        "is_paused": True,
        "pending_action": {
            "tool_name": "return_init",
            "args": {"order_id": "BH-123"},
            "id": "call_abc",
            "paused_at": 1700000000.0
        }
    }

    async def mock_get_paused():
        return [dummy_item]

    monkeypatch.setattr("main.get_all_paused_sessions", mock_get_paused)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/admin/pending-approvals", headers={"X-Admin-Key": "l1support-demo-key-2026"})
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 1
        assert data["items"][0]["session_id"] == "session_abc"
        assert data["items"][0]["pending_action"]["tool_name"] == "return_init"


@pytest.mark.asyncio
async def test_review_endpoint_metrics(monkeypatch):
    """Verify that calling review endpoint records decisions and latency metrics."""
    import time
    paused_time = time.time() - 10.0  # Paused 10 seconds ago

    mock_session = {
        "session_id": "session_metrics_test",
        "brand_id": "boohoo_uk",
        "channel": "chat",
        "customer_id": "cust_metrics",
        "messages": [],
        "turn_count": 0,
        "is_paused": True,
        "pending_action": {
            "tool_name": "return_init",
            "args": {"order_id": "BH-123"},
            "id": "call_metrics",
            "paused_at": paused_time,
        },
        "human_approval": None,
        "human_feedback": None,
    }

    async def mock_get(sid):
        return mock_session if sid == "session_metrics_test" else None

    async def mock_update(sid, data):
        mock_session.update(data)

    monkeypatch.setattr("main.get_session", mock_get)
    monkeypatch.setattr("main.update_session", mock_update)

    # Mock get_graph to bypass real execution
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="Resumed successfully!")],
            "turn_count": 2,
            "resolution_attempts": 1,
            "outcome": None,
            "is_paused": False,
            "pending_action": None,
            "human_approval": None,
            "human_feedback": None,
        }
    )
    monkeypatch.setattr("main.get_graph", lambda: mock_graph)

    # Initial metrics check
    decisions_metric = APPROVAL_DECISIONS_TOTAL.labels(
        brand_id="boohoo_uk",
        tool_name="return_init",
        decision="approved"
    )
    assert decisions_metric._value.get() == 0

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/session/session_metrics_test/review",
            json={"decision": "approved", "feedback": "supervisor review"},
        )
        assert res.status_code == 200

    # Verify that the decision counter was incremented
    assert decisions_metric._value.get() == 1

    # Verify that latency metric was recorded
    latency_metric = APPROVAL_LATENCY_SECONDS.labels(
        brand_id="boohoo_uk",
        tool_name="return_init",
        decision="approved"
    )
    assert latency_metric._sum.get() >= 10.0
