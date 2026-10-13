"""Tests for CSAT-Driven Dynamic SLA Escalation (Phase 29)."""

from __future__ import annotations

import json
import pytest
from core.orchestration.csat_sla import is_customer_high_churn_risk
from core.orchestration.sla_monitor import check_sla_status


@pytest.mark.asyncio
async def test_is_customer_high_churn_risk_negative_csat(monkeypatch):
    """Verify that a customer with average CSAT < 3.0 is flagged as high churn risk."""
    async def mock_get_history(customer_id):
        return [
            {"session_id": "s1", "outcome": "resolved", "satisfaction": 2.0},
            {"session_id": "s2", "outcome": "resolved", "satisfaction": 3.0},
        ]
    monkeypatch.setattr("core.orchestration.csat_sla.get_customer_history", mock_get_history)

    is_risk = await is_customer_high_churn_risk("cust_bad_csat")
    assert is_risk is True


@pytest.mark.asyncio
async def test_is_customer_high_churn_risk_prior_escalation(monkeypatch):
    """Verify that a customer with prior escalated outcome is flagged as high churn risk."""
    async def mock_get_history(customer_id):
        return [
            {"session_id": "s1", "outcome": "escalated", "satisfaction": None},
        ]
    monkeypatch.setattr("core.orchestration.csat_sla.get_customer_history", mock_get_history)

    is_risk = await is_customer_high_churn_risk("cust_escaped")
    assert is_risk is True


@pytest.mark.asyncio
async def test_is_customer_high_churn_risk_safe(monkeypatch):
    """Verify that a customer with clean history is not flagged."""
    async def mock_get_history(customer_id):
        return [
            {"session_id": "s1", "outcome": "resolved", "satisfaction": 4.5},
            {"session_id": "s2", "outcome": "resolved", "satisfaction": 5.0},
        ]
    monkeypatch.setattr("core.orchestration.csat_sla.get_customer_history", mock_get_history)

    is_risk = await is_customer_high_churn_risk("cust_happy")
    assert is_risk is False


@pytest.mark.asyncio
async def test_check_sla_status_standard_thresholds(monkeypatch):
    """Verify standard SLA warning and breach thresholds apply normally."""
    # Mock session loader to return no customer
    async def mock_get_session(sid):
        return {"session_id": sid, "customer_id": None}
    monkeypatch.setattr("core.memory.session.get_session", mock_get_session)

    # Standard warning: turn_count > 5
    status_ok = await check_sla_status("sess_std", turn_count=3, escalation_risk=0.10)
    assert status_ok == "ok"

    status_warning = await check_sla_status("sess_std", turn_count=6, escalation_risk=0.10)
    assert status_warning == "warning"

    status_breached = await check_sla_status("sess_std", turn_count=9, escalation_risk=0.10)
    assert status_breached == "breached"


@pytest.mark.asyncio
async def test_check_sla_status_priority_thresholds(monkeypatch):
    """Verify priority SLA warning and breach thresholds apply for high-risk customers."""
    # Mock session loader to return a customer_id
    async def mock_get_session(sid):
        return {"session_id": sid, "customer_id": "cust_bad_csat"}
    monkeypatch.setattr("core.memory.session.get_session", mock_get_session)

    # Mock risk evaluator
    async def mock_is_risk(cid):
        return True
    monkeypatch.setattr("core.orchestration.csat_sla.is_customer_high_churn_risk", mock_is_risk)

    session_updates = {}
    async def mock_update(sid, updates):
        session_updates.update(updates)
    monkeypatch.setattr("core.memory.session.update_session", mock_update)

    # Priority warning: turn_count > 2
    status_ok = await check_sla_status("sess_priority", turn_count=2, escalation_risk=0.10)
    assert status_ok == "ok"

    status_warning = await check_sla_status("sess_priority", turn_count=3, escalation_risk=0.10)
    assert status_warning == "warning"

    # Priority breach: turn_count > 4
    status_breached = await check_sla_status("sess_priority", turn_count=5, escalation_risk=0.10)
    assert status_breached == "breached"

    # Verify is_priority_sla is stored in session details
    assert session_updates.get("is_priority_sla") is True
