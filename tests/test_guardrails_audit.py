"""Tests for Cognitive Guardrails and Automated Compliance Audit Logs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from core.ontology.models import Channel, ContextPackage, IntentResult, SessionOutcome
from core.orchestration.graph import node_guardrail, node_close_session, node_escalate
from core.orchestration.state import L1SupportState
from langchain_core.messages import AIMessage, HumanMessage
from httpx import ASGITransport, AsyncClient
from main import app


@pytest.mark.asyncio
async def test_node_guardrail_pass_clean(monkeypatch):
    """Verify guardrail passes clean responses without modifications."""
    mock_llm = MagicMock()
    mock_llm.get_lc_client = MagicMock(return_value=mock_llm)
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content='{"passed": true, "hallucination_reason": null, "brand_voice_reason": null, "corrected_response": null}'
        )
    )
    monkeypatch.setattr("core.orchestration.llm.get_llm_client", lambda: mock_llm)

    state = L1SupportState(
        session_id="test_session_guardrail",
        brand_id="boohoo_uk",
        channel=Channel.CHAT,
        messages=[HumanMessage(content="Hello"), AIMessage(content="Hello, how can I help you?")],
        compliance_score=1.0,
    )

    updates = await node_guardrail(state)
    assert updates.get("compliance_score") == 1.0
    assert "messages" not in updates  # No modification


@pytest.mark.asyncio
async def test_node_guardrail_fail_and_rewrite(monkeypatch):
    """Verify guardrail catches hallucinations, rewrites content, and flags compliance."""
    mock_llm = MagicMock()
    mock_llm.get_lc_client = MagicMock(return_value=mock_llm)
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content='{"passed": false, "hallucination_reason": "Claimed refund takes 3 days", "brand_voice_reason": null, "corrected_response": "Refunds take 14 days."}'
        )
    )
    monkeypatch.setattr("core.orchestration.llm.get_llm_client", lambda: mock_llm)

    state = L1SupportState(
        session_id="test_session_guardrail",
        brand_id="boohoo_uk",
        channel=Channel.CHAT,
        messages=[HumanMessage(content="When is my refund?"), AIMessage(content="Your refund takes 3 days.")],
        compliance_score=1.0,
    )

    updates = await node_guardrail(state)
    assert updates.get("compliance_score") == 0.5
    assert "compliance_violations" in updates
    assert "Claimed refund takes 3 days" in updates["compliance_violations"]
    assert len(updates["messages"]) == 1
    assert updates["messages"][0].content == "Refunds take 14 days."


@pytest.mark.asyncio
async def test_audit_log_written_on_close(monkeypatch):
    """Verify that when node_close_session is called, an audit log is persisted to PostgreSQL."""
    write_called = False

    def mock_write(session_id, brand_id, customer_id, intent_trajectory, tools_executed, policies_matched, compliance_score, compliance_violations, audit_summary):
        nonlocal write_called
        write_called = True
        assert session_id == "test_session_close_audit"
        assert brand_id == "boohoo_uk"
        assert compliance_score == 1.0
        assert "resolved successfully" in audit_summary

    monkeypatch.setattr("feedback.engine.write_session_audit_log", mock_write)
    monkeypatch.setattr("core.memory.session.get_session", AsyncMock(return_value=None))

    state = L1SupportState(
        session_id="test_session_close_audit",
        brand_id="boohoo_uk",
        channel=Channel.CHAT,
        messages=[AIMessage(content="All done!")],
        turn_count=2,
        intent=IntentResult(issue_type="returns", category="account", confidence=1.0),
    )

    await node_close_session(state)
    assert write_called is True


@pytest.mark.asyncio
async def test_audit_logs_api_endpoint(monkeypatch):
    """Verify GET /admin/audit-logs retrieves logged trace items correctly."""
    dummy_log = {
        "id": 42,
        "session_id": "session_audit_123",
        "brand_id": "boohoo_uk",
        "customer_id": "cust_456",
        "intent_trajectory": ["returns"],
        "tools_executed": ["return_init"],
        "policies_matched": ["returns"],
        "compliance_score": 1.0,
        "compliance_violations": None,
        "audit_summary": "Resolved successfully",
        "created_at": "2026-07-02T12:00:00"
    }

    def mock_get(limit, offset, brand_id):
        return {"total": 1, "items": [dummy_log], "limit": limit, "offset": offset}

    monkeypatch.setattr("feedback.engine.get_all_audit_logs", mock_get)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/admin/audit-logs", headers={"X-Admin-Key": "l1support-demo-key-2026"})
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 1
        assert data["items"][0]["session_id"] == "session_audit_123"
        assert data["items"][0]["compliance_score"] == 1.0
