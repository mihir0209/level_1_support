"""Tests for Agentic Self-Healing and Exception Auto-Tuning."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.knowledge.retriever import get_return_policy
from core.orchestration.graph import StatefulToolNode
from core.orchestration.llm import LLMClient
from langchain_core.messages import AIMessage, ToolMessage


@pytest.mark.asyncio
async def test_llm_self_heal_json(monkeypatch):
    """Verify that self_heal_json corrects malformed JSON string outputs."""
    client = LLMClient()

    # Mock the LLM to output a corrected valid JSON payload
    monkeypatch.setattr(
        client,
        "generate",
        AsyncMock(return_value='{"issue_type": "wismo", "confidence": 0.95}'),
    )

    res = await client.self_heal_json(
        '{"issue_type": "wismo", "confidence": 0.95', "Unexpected end of JSON input"
    )
    assert res is not None
    assert res["issue_type"] == "wismo"
    assert res["confidence"] == 0.95


@pytest.mark.asyncio
async def test_stateful_tool_node_self_healing(monkeypatch):
    """Verify that StatefulToolNode traps bad tool call parameters, runs reflection, and retries."""
    node = StatefulToolNode()

    # Mock an available tool that fails on first attempt but succeeds on corrected retry
    mock_tool = MagicMock()
    mock_tool.name = "mock_test_tool"
    mock_tool.args = {"arg1": {"type": "string"}}
    mock_tool.ainvoke = AsyncMock(side_effect=[Exception("Schema mismatch"), "success_res"])

    # Mock tool resolution to yield our mock tool
    monkeypatch.setattr(node, "_resolve_tools", lambda state: [mock_tool])

    # Mock parameters self-correction to return corrected parameters
    node._self_correct_tool_args = AsyncMock(return_value={"arg1": "val"})

    # Setup mock support state
    mock_state = MagicMock()
    mock_state.session_id = "test_self_healing_session"

    ai_msg = AIMessage(
        content="Calling mock tool.",
        tool_calls=[{"name": "mock_test_tool", "args": {"arg1": 123}, "id": "call_123"}],
    )
    mock_state.messages = [ai_msg]

    res = await node._async_invoke(mock_state)
    messages = res["messages"]
    assert len(messages) == 1
    assert messages[0].content == "success_res"
    assert mock_tool.ainvoke.call_count == 2


@pytest.mark.asyncio
async def test_policy_retriever_redis_failover(monkeypatch):
    """Verify get_return_policy falls back to Redis cached policies when FalkorDB queries fail."""
    # Mock redis instance returning cached policy payload
    mock_redis = MagicMock()
    mock_redis.get.return_value = '{"return_window_days": 28, "return_cost_gbp": 2.50, "return_cost_usd": 3.50, "free_return_condition": "none", "excluded_categories": [], "effective_date": "2026-01-01", "refund_timeline_days": 14}'

    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: mock_redis)

    # Mock get_graph_client to raise a DB Connection exception simulating database outage
    monkeypatch.setattr(
        "core.knowledge.retriever.get_graph_client",
        MagicMock(side_effect=Exception("Connection to FalkorDB timed out.")),
    )

    res = get_return_policy("boohoo_uk")
    assert res is not None
    assert res["return_window_days"] == 28
    assert res["return_cost_gbp"] == 2.50
    mock_redis.get.assert_called_once()
