"""Tests for WebSocket Real-time Live Chat Monitoring."""

from __future__ import annotations

import pytest
from main import app, ws_manager
from fastapi.testclient import TestClient


def test_websocket_connection_and_broadcast():
    """Verify that supervisors can connect via WebSockets and receive broadcasts."""
    client = TestClient(app)
    
    # Connect
    with client.websocket_connect("/admin/ws") as websocket:
        # 1. Test ping-pong heartbeat
        websocket.send_text("ping")
        resp = websocket.receive_text()
        assert resp == "pong"

        # 2. Test event broadcasting
        import asyncio
        loop = ws_manager.active_connections[0].custom_loop
        future = asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast({
                "event": "test_event",
                "data": {"status": "ok"}
            }),
            loop
        )
        future.result()

        # Read broadcast message
        data = websocket.receive_json()
        assert data["event"] == "test_event"
        assert data["data"]["status"] == "ok"


@pytest.mark.asyncio
async def test_websocket_triggers_on_safety_shield(monkeypatch):
    """Verify safety shield trigger sends real-time safety_alert broadcasts."""
    broadcasts = []

    async def mock_broadcast(message):
        broadcasts.append(message)

    async def mock_safety(text):
        return (False, "mock safety trigger")

    monkeypatch.setattr(ws_manager, "broadcast", mock_broadcast)
    monkeypatch.setattr("core.middleware.safety_shield.check_input_safety", mock_safety)

    class MockRedis:
        async def get(self, key):
            # Session exists
            return '{"session_id": "ws_session_1", "brand_id": "boohoo_uk", "channel": "chat", "messages": []}'
        async def set(self, key, val):
            return True

    monkeypatch.setattr("core.memory.session.get_redis", lambda: MockRedis())

    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # POST chat turn that triggers safety shield failure
        res = await client.post(
            "/chat",
            json={"session_id": "ws_session_1", "message": "drop tables;"},
            headers={"X-API-KEY": "l1support-demo-key-2026"}
        )
        assert res.status_code == 200
        
        # Verify both safety_alert and session_updated events were broadcasted
        assert len(broadcasts) == 2
        events = [b["event"] for b in broadcasts]
        assert "safety_alert" in events
        assert "session_updated" in events
