"""Tests for Multi-Agent Parallel Task Delegation & Coordinator Graph."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from core.orchestration.coordinator import decompose_compound_request
from main import app
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_decompose_compound_request(monkeypatch):
    """Verify that decompose_compound_request splits a composite statement into distinct tasks."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        return_value="""
        [
          {
            "task_type": "shipping_wismo",
            "sub_query": "where is order 123",
            "explanation": "tracking query"
          },
          {
            "task_type": "returns",
            "sub_query": "return order 456",
            "explanation": "return query"
          }
        ]
        """
    )
    monkeypatch.setattr("core.orchestration.coordinator.get_llm_client", lambda: mock_llm)

    tasks = await decompose_compound_request("where is order 123 and return order 456")
    assert len(tasks) == 2
    assert tasks[0]["task_type"] == "shipping_wismo"
    assert tasks[1]["task_type"] == "returns"


@pytest.mark.asyncio
async def test_chat_returns_decomposed_subtasks(monkeypatch):
    """Verify that chat responses propagate the sub-tasks payload."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        return_value="[]"  # standard single task
    )
    monkeypatch.setattr("core.orchestration.coordinator.get_llm_client", lambda: mock_llm)

    # Mock graph runner output
    class MockGraph:
        async def ainvoke(self, state, config=None):
            # Return same state with updated turn count using Pydantic model_copy
            return state.model_copy(update={"turn_count": state.turn_count + 1})

    monkeypatch.setattr("main.get_graph", lambda: MockGraph())

    # Mock Redis session
    mock_session = {
        "session_id": "test_delegation_session",
        "brand_id": "boohoo_uk",
        "channel": "chat",
        "turn_count": 0,
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
        res = await client.post(
            "/chat",
            json={
                "session_id": "test_delegation_session",
                "message": "hello",
                "customer_id": "cust_123"
            }
        )
        assert res.status_code == 200
        data = res.json()
        assert "sub_tasks" in data
        assert isinstance(data["sub_tasks"], list)
