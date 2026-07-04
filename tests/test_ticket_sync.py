"""Tests for CRM Ticket sync Loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from main import app
from core.crm.ticket_sync import (
    get_crm_config,
    save_crm_config,
    get_crm_sync_logs,
    sync_session_to_crm,
)
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_crm_config_store(monkeypatch):
    """Verify set and get configurations persistence inside Redis."""
    db_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.crm.ticket_sync.get_redis", lambda: MockRedis())

    # Save
    await save_crm_config("https://my-zendesk.com", "my-token")
    assert db_store["crm:config:webhook_url"] == "https://my-zendesk.com"
    assert db_store["crm:config:auth_token"] == "my-token"

    # Get
    config = await get_crm_config()
    assert config["webhook_url"] == "https://my-zendesk.com"
    assert config["auth_token"] == "my-token"


@pytest.mark.asyncio
async def test_crm_sync_dispatcher(monkeypatch):
    """Verify that sync_session_to_crm constructs ticket payload and pushes audit logs."""
    db_store = {}
    logs = []

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def lpush(self, key, val):
            logs.append(val)
            return len(logs)
        async def ltrim(self, key, start, stop):
            return True

    monkeypatch.setattr("core.crm.ticket_sync.get_redis", lambda: MockRedis())

    # Sync
    res = await sync_session_to_crm(
        session_id="sync_session_99",
        brand_id="boohoo_uk",
        session_data={
            "messages": [{"role": "user", "content": "Need help"}],
            "turn_count": 1,
            "outcome": "resolved",
            "escalation_risk": 0.1,
            "compliance_score": 1.0,
            "sla_status": "ok"
        }
    )

    assert res["session_id"] == "sync_session_99"
    assert res["status"] == "success"
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_crm_api_endpoints(monkeypatch):
    """Verify admin config and audit log retrieval endpoints."""
    db_store = {
        "crm:config:webhook_url": "https://my-salesforce.com",
        "crm:config:auth_token": "sf-token-999"
    }
    logs = [
        '{"session_id": "s_sync", "brand_id": "boohoo_uk", "status": "success", "latency_ms": 12.0, "timestamp": 1783012345}'
    ]

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True
        async def lrange(self, key, start, stop):
            return logs

    monkeypatch.setattr("core.crm.ticket_sync.get_redis", lambda: MockRedis())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET Config
        res = await client.get(
            "/admin/crm/config",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        assert res.json()["webhook_url"] == "https://my-salesforce.com"

        # 2. POST Config
        res_post = await client.post(
            "/admin/crm/config",
            json={"webhook_url": "https://new-url.com", "auth_token": "new-token"},
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_post.status_code == 200
        assert db_store["crm:config:webhook_url"] == "https://new-url.com"

        # 3. GET logs
        res_logs = await client.get(
            "/admin/crm/logs",
            headers={"X-Admin-Key": "l1support-demo-key-2026"}
        )
        assert res_logs.status_code == 200
        data_logs = res_logs.json()
        assert data_logs["count"] == 1
        assert data_logs["items"][0]["session_id"] == "s_sync"
