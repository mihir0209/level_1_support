"""Tests for Dynamic SLA Compliance & Session Alerting Engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from main import app
from core.orchestration.sla_monitor import check_sla_status
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_sla_compliance_monitor_rules():
    """Verify rules-based SLA warning and breach threshold transitions."""
    # 1. Base compliant case
    status = await check_sla_status("s1", turn_count=2, escalation_risk=0.1)
    assert status == "ok"

    # 2. Turn count warning
    status = await check_sla_status("s2", turn_count=6, escalation_risk=0.1)
    assert status == "warning"

    # 3. Escalation risk warning
    status = await check_sla_status("s3", turn_count=2, escalation_risk=0.8)
    assert status == "warning"

    # 4. Turn count breach
    status = await check_sla_status("s4", turn_count=9, escalation_risk=0.1)
    assert status == "breached"

    # 5. Escalation risk breach
    status = await check_sla_status("s5", turn_count=2, escalation_risk=0.95)
    assert status == "breached"


@pytest.mark.asyncio
async def test_sla_alerts_and_snooze_endpoints(monkeypatch):
    """Verify retrieval of warning/breached SLA sessions and snooze overrides."""
    # Mock Redis data store
    db_store = {
        "session:s_ok": """
        {
          "session_id": "s_ok",
          "brand_id": "boohoo_uk",
          "channel": "chat",
          "turn_count": 2,
          "sla_status": "ok"
        }
        """,
        "session:s_warning": """
        {
          "session_id": "s_warning",
          "brand_id": "boohoo_uk",
          "channel": "chat",
          "turn_count": 6,
          "sla_status": "warning",
          "messages": []
        }
        """,
        "session:s_breached": """
        {
          "session_id": "s_breached",
          "brand_id": "boohoo_uk",
          "channel": "chat",
          "turn_count": 9,
          "sla_status": "breached",
          "messages": []
        }
        """
    }

    class MockRedis:
        async def scan_iter(self, match):
            for k in db_store.keys():
                yield k
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.memory.session.get_redis", lambda: MockRedis())

    async def mock_get_session(sid):
        import json
        raw = db_store.get(f"session:{sid}")
        return json.loads(raw) if raw else None
    async def mock_update_session(sid, data):
        import json
        db_store[f"session:{sid}"] = json.dumps(data)
        return True

    monkeypatch.setattr("main.get_session", mock_get_session)
    monkeypatch.setattr("main.update_session", mock_update_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Test alert list endpoint
        res = await client.get(
            "/admin/sla/alerts",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 2
        items = data["items"]
        assert any(i["session_id"] == "s_warning" for i in items)
        assert any(i["session_id"] == "s_breached" for i in items)

        # 2. Test snooze endpoint
        res_snooze = await client.post(
            "/admin/sla/snooze",
            json={"session_id": "s_warning"},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_snooze.status_code == 200
        assert res_snooze.json()["status"] == "success"

        # Verify it was reset to 'ok' in db
        import json
        updated = json.loads(db_store["session:s_warning"])
        assert updated["sla_status"] == "ok"
