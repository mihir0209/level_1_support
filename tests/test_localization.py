"""Tests for multi-lingual and localization support in the orchestrator flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from core.orchestration.graph import (
    node_assemble_context,
    node_generate_response,
)
from core.orchestration.llm import LLMClient
from core.orchestration.state import L1SupportState
from langchain_core.messages import AIMessage, HumanMessage


@pytest.mark.asyncio
async def test_llm_detect_language(monkeypatch):
    """Verify that language detection correctly classifies user queries."""
    client = LLMClient()

    async_mock = AsyncMock(return_value="es")
    monkeypatch.setattr(client, "generate", async_mock)

    lang = await client.detect_language("¿Dónde está mi pedido?")
    assert lang == "es"
    assert async_mock.called


@pytest.mark.asyncio
async def test_llm_translate_content(monkeypatch):
    """Verify that content translation correctly converts messages."""
    client = LLMClient()

    async_mock = AsyncMock(return_value="Where is my order?")
    monkeypatch.setattr(client, "generate", async_mock)

    translation = await client.translate_content("¿Dónde está mi pedido?", "en")
    assert translation == "Where is my order?"
    assert async_mock.called


@pytest.mark.asyncio
async def test_node_assemble_context_localization(monkeypatch):
    """Verify that non-English messages are translated to English before context matching."""
    mock_llm = MagicMock()
    mock_llm.detect_language = AsyncMock(return_value="es")
    mock_llm.translate_content = AsyncMock(
        side_effect=lambda text, target: "Where is my order?" if target == "en" else text
    )

    monkeypatch.setattr("core.orchestration.graph.get_llm_client", lambda: mock_llm)

    mock_context_pkg = MagicMock()
    mock_context_pkg.intent = MagicMock(issue_type="wismo", confidence=0.9)
    mock_context_pkg.retrieval_confidence = 0.8
    mock_context_pkg.entities = MagicMock()

    mock_assemble = AsyncMock(return_value=mock_context_pkg)
    monkeypatch.setattr("core.orchestration.graph.assemble_context", mock_assemble)

    state = L1SupportState(
        session_id="test-session-123",
        brand_id="boohoo_uk",
        channel="chat",
        messages=[HumanMessage(content="¿Dónde está mi pedido?")]
    )

    res = await node_assemble_context(state)
    assert res["locale"] == "es"

    mock_assemble.assert_called_once()
    kwargs = mock_assemble.call_args[1]
    assert kwargs["message"] == "Where is my order?"


@pytest.mark.asyncio
async def test_node_generate_response_localization(monkeypatch):
    """Verify that final response content is translated back to the customer's native language."""
    mock_llm = MagicMock()
    mock_llm.translate_content = AsyncMock(return_value="Su pedido está en camino.")

    monkeypatch.setattr("core.orchestration.graph.get_llm_client", lambda: mock_llm)

    mock_lc_client = MagicMock()
    mock_response = AIMessage(content="Your order is on the way.")
    mock_lc_client.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.get_lc_client.return_value = mock_lc_client

    monkeypatch.setattr("core.orchestration.graph._build_llm_messages", AsyncMock(return_value=[]))

    state = L1SupportState(
        session_id="test-session-123",
        brand_id="boohoo_uk",
        channel="chat",
        locale="es",
        messages=[HumanMessage(content="¿Dónde está mi pedido?")]
    )

    res = await node_generate_response(state)
    messages = res["messages"]
    assert len(messages) == 1
    assert messages[0].content == "Su pedido está en camino."
