"""Tests for Proactive Churn Risk Alert Webhooks & Slack Integration (Phase 32)."""

from __future__ import annotations

import json
import pytest
from core.orchestration.churn_alerts import broadcast_churn_alert


@pytest.mark.asyncio
async def test_broadcast_churn_alert_generic(monkeypatch):
    """Verify generic webhook format payload is sent correctly."""
    monkeypatch.setattr("config.settings.churn_alert_webhook_url", "https://api.example.com/webhooks/churn")

    posted_url = None
    posted_payload = None

    class MockResponse:
        status_code = 200
        text = "OK"

    async def mock_post(self, url, json, timeout=None):
        nonlocal posted_url, posted_payload
        posted_url = url
        posted_payload = json
        return MockResponse()

    # Monkeypatch httpx AsyncClient post
    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    await broadcast_churn_alert(
        session_id="sess_123",
        customer_id="cust_bad",
        brand_id="boohoo_uk",
        average_csat=2.5
    )

    assert posted_url == "https://api.example.com/webhooks/churn"
    assert posted_payload is not None
    assert posted_payload["event"] == "churn_risk_sla_alert"
    assert posted_payload["session_id"] == "sess_123"
    assert posted_payload["customer_id"] == "cust_bad"
    assert posted_payload["brand_id"] == "boohoo_uk"
    assert posted_payload["average_csat"] == 2.5


@pytest.mark.asyncio
async def test_broadcast_churn_alert_slack(monkeypatch):
    """Verify Slack block kit payload format is sent for hooks.slack.com."""
    monkeypatch.setattr("config.settings.churn_alert_webhook_url", "https://hooks.slack.com/services/T00/B00/X00")

    posted_payload = None

    class MockResponse:
        status_code = 200
        text = "ok"

    async def mock_post(self, url, json, timeout=None):
        nonlocal posted_payload
        posted_payload = json
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    await broadcast_churn_alert(
        session_id="sess_slack",
        customer_id="cust_slack",
        brand_id="boohoo_us",
        average_csat=1.75
    )

    assert posted_payload is not None
    assert "blocks" in posted_payload
    blocks = posted_payload["blocks"]
    assert len(blocks) == 2
    assert blocks[0]["type"] == "section"
    assert "Churn Risk Alert" in blocks[0]["text"]["text"]
    
    fields = blocks[1]["fields"]
    # Check that session ID and brand ID are in the blocks
    assert any("sess_slack" in f["text"] for f in fields)
    assert any("boohoo_us" in f["text"] for f in fields)
    assert any("1.75" in f["text"] for f in fields)


@pytest.mark.asyncio
async def test_broadcast_churn_alert_no_url(monkeypatch):
    """Verify webhook broadcast returns immediately if no webhook URL is configured."""
    monkeypatch.setattr("config.settings.churn_alert_webhook_url", "")

    post_called = False

    async def mock_post(*args, **kwargs):
        nonlocal post_called
        post_called = True

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    await broadcast_churn_alert(
        session_id="sess_123",
        customer_id="cust_bad",
        brand_id="boohoo_uk",
        average_csat=2.5
    )

    assert post_called is False
