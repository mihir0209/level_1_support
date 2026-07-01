"""Unit and integration tests for Human-in-the-Loop action approval gates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from core.agents.simple import HighRiskActionPaused, HighRiskActionRejected, SimpleAgent
from core.orchestration.state import L1SupportState
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from main import app


@pytest.mark.asyncio
async def test_simple_agent_hitl_pending_raises_paused(monkeypatch):
    """Verify SimpleAgent raises HighRiskActionPaused when approval is None."""
    # Create agent with a high-risk tool return_init
    mock_tool = MagicMock()
    mock_tool.name = "return_init"
    agent = SimpleAgent(
        name="Test-Agent",
        description="Test Returns",
        system_prompt="Always call return_init.",
        tools=[mock_tool],
    )

    # Mock the LLM to output a tool call to return_init
    mock_lc = MagicMock()
    mock_lc.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[{"name": "return_init", "args": {"order_id": "BH-1"}, "id": "call_1"}],
        )
    )
    mock_lc.bind_tools = MagicMock(return_value=mock_lc)
    monkeypatch.setattr(agent, "tools", [mock_tool])

    # Setup mock LLM client
    mock_llm = MagicMock()
    mock_llm.get_lc_client.return_value = mock_lc
    monkeypatch.setattr("core.agents.simple.get_llm_client", lambda: mock_llm)

    # 1. Test Pending (None) -> raises HighRiskActionPaused
    state = L1SupportState(
        session_id="session_1",
        brand_id="boohoo_uk",
        channel="chat",
        human_approval=None,
    )
    with pytest.raises(HighRiskActionPaused) as exc:
        await agent.run("Initiate return", state=state)
    assert exc.value.tool_name == "return_init"
    assert exc.value.tool_args == {"order_id": "BH-1"}


@pytest.mark.asyncio
async def test_simple_agent_hitl_rejected_raises_rejected(monkeypatch):
    """Verify SimpleAgent raises HighRiskActionRejected when approval is rejected."""
    mock_tool = MagicMock()
    mock_tool.name = "return_init"
    agent = SimpleAgent(
        name="Test-Agent",
        description="Test Returns",
        system_prompt="Always call return_init.",
        tools=[mock_tool],
    )

    mock_lc = MagicMock()
    mock_lc.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[{"name": "return_init", "args": {"order_id": "BH-1"}, "id": "call_1"}],
        )
    )
    mock_lc.bind_tools = MagicMock(return_value=mock_lc)
    mock_llm = MagicMock()
    mock_llm.get_lc_client.return_value = mock_lc
    monkeypatch.setattr("core.agents.simple.get_llm_client", lambda: mock_llm)

    # 2. Test Rejected -> raises HighRiskActionRejected
    state = L1SupportState(
        session_id="session_1",
        brand_id="boohoo_uk",
        channel="chat",
        human_approval="rejected",
        human_feedback="Fraud detected",
    )
    with pytest.raises(HighRiskActionRejected) as exc:
        await agent.run("Initiate return", state=state)
    assert exc.value.tool_name == "return_init"
    assert exc.value.feedback == "Fraud detected"


@pytest.mark.asyncio
async def test_simple_agent_hitl_approved_executes_tool(monkeypatch):
    """Verify SimpleAgent executes tool when approval is approved."""
    mock_tool = MagicMock()
    mock_tool.name = "return_init"
    mock_tool.invoke = MagicMock(return_value="Return successful!")
    agent = SimpleAgent(
        name="Test-Agent",
        description="Test Returns",
        system_prompt="Always call return_init.",
        tools=[mock_tool],
    )

    mock_lc = MagicMock()
    # First turn outputs tool call, second turn outputs text response
    mock_lc.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[{"name": "return_init", "args": {"order_id": "BH-1"}, "id": "call_1"}],
            ),
            AIMessage(content="Return done."),
        ]
    )
    mock_lc.bind_tools = MagicMock(return_value=mock_lc)
    mock_llm = MagicMock()
    mock_llm.get_lc_client.return_value = mock_lc
    monkeypatch.setattr("core.agents.simple.get_llm_client", lambda: mock_llm)

    # 3. Test Approved -> runs successfully
    state = L1SupportState(
        session_id="session_1",
        brand_id="boohoo_uk",
        channel="chat",
        human_approval="approved",
    )
    res = await agent.run("Initiate return", state=state)
    assert res == "Return done."
    assert mock_tool.invoke.call_count == 1


@pytest.mark.asyncio
async def test_hitl_review_endpoint(monkeypatch):
    """Verify FastAPI /session/{session_id}/review endpoint transitions state correctly."""
    mock_session = {
        "session_id": "test_hitl_session",
        "brand_id": "boohoo_uk",
        "channel": "chat",
        "customer_id": "cust_001",
        "messages": [],
        "turn_count": 0,
        "is_paused": True,
        "pending_action": {
            "tool_name": "return_init",
            "args": {"order_id": "BH-12345-UK"},
            "id": "call_hitl",
        },
        "human_approval": None,
        "human_feedback": None,
    }

    async def mock_get(sid):
        return mock_session if sid == "test_hitl_session" else None

    async def mock_update(sid, data):
        mock_session.update(data)

    monkeypatch.setattr("main.get_session", mock_get)
    monkeypatch.setattr("main.update_session", mock_update)

    # Mock get_graph to return a dummy compiled graph
    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="Resumed and return executed successfully!")],
            "turn_count": 2,
            "resolution_attempts": 1,
            "outcome": MagicMock(value="resolved"),
            "is_paused": False,
            "pending_action": None,
            "human_approval": None,
            "human_feedback": None,
        }
    )
    monkeypatch.setattr("main.get_graph", lambda: mock_graph)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        review_res = await client.post(
            "/session/test_hitl_session/review",
            json={"decision": "approved", "feedback": "Approved by supervisor"},
        )
        assert review_res.status_code == 200
        review_data = review_res.json()
        assert review_data["is_paused"] is False
        assert review_data["response"] == "Resumed and return executed successfully!"
        assert mock_session["is_paused"] is False
        assert mock_session["human_approval"] is None
