"""Tests for Predictive Escalation Risk and Supervisor Takeover features."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from core.ontology.models import Channel
from main import app, calculate_escalation_risk, get_session
from httpx import ASGITransport, AsyncClient


def test_calculate_escalation_risk():
    """Verify predictive escalation risk calculations."""
    # Under 4 turns, happy, 0 resolution attempts -> 0.0 risk
    assert calculate_escalation_risk(2, "happy", 0) == 0.0

    # Frustrated sentiment adds 0.35
    assert calculate_escalation_risk(3, "frustrated", 0) == 0.35

    # Turn count scaling after 4 turns
    assert calculate_escalation_risk(6, "neutral", 0) == pytest.approx(0.20)

    # Combined risk: frustrated, 6 turns, 2 resolution attempts
    # frustrated: 0.35
    # 6 turns (6-4) * 0.10: 0.20
    # 2 attempts: 2 * 0.15 = 0.30
    # total = 0.85
    assert calculate_escalation_risk(6, "frustrated", 2) == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_session_takeover_and_intercept(monkeypatch):
    """Verify session takeover endpoints lock AI and intercept messages correctly."""
    # Mock session data store
    mock_session = {
        "session_id": "test_takeover_123",
        "brand_id": "boohoo_uk",
        "channel": "chat",
        "turn_count": 2,
        "messages": [
            {"role": "user", "content": "hello"}
        ],
        "is_takeover": False,
        "escalation_risk": 0.10,
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
        # 1. Toggle Takeover to True
        res_takeover = await client.post(
            "/admin/session/test_takeover_123/takeover",
            json={"is_takeover": True},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_takeover.status_code == 200
        assert res_takeover.json()["is_takeover"] is True
        assert mock_session["is_takeover"] is True

        # 2. Intercept message
        res_intercept = await client.post(
            "/admin/session/test_takeover_123/intercept",
            json={"message": "Supervisor intervention here."},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_intercept.status_code == 200
        assert res_intercept.json()["message"] == "Supervisor intervention here."
        assert mock_session["messages"][-1]["content"] == "Supervisor intervention here."
        assert mock_session["messages"][-1]["role"] == "assistant"
        assert mock_session["is_takeover"] is True

        # 3. Call /chat endpoint when is_takeover is True — verify AI bypass
        res_chat = await client.post(
            "/chat",
            json={
                "session_id": "test_takeover_123",
                "message": "can you help me?",
                "customer_id": "cust_123"
            }
        )
        assert res_chat.status_code == 200
        data_chat = res_chat.json()
        assert data_chat["is_takeover"] is True
        assert data_chat["response"] == "A human supervisor has taken over this chat to assist you directly."
