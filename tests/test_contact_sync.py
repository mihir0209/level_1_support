"""Tests for Chatwoot Webhook Contact Synchronization."""

from __future__ import annotations

import json
import pytest
from core.crm.contact_sync import (
    extract_contact_from_payload,
    resolve_or_create_customer,
    sync_contact_from_webhook,
)


def test_extract_contact_from_payload_full():
    """Verify extraction of name, email, phone from a complete Chatwoot payload."""
    payload = {
        "event": "message_created",
        "message_type": "incoming",
        "sender": {
            "id": 42,
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone_number": "+447700900000",
        },
        "conversation": {"id": 101, "inbox_id": 1},
    }
    contact = extract_contact_from_payload(payload)
    assert contact is not None
    assert contact["name"] == "Jane Doe"
    assert contact["email"] == "jane@example.com"
    assert contact["phone"] == "+447700900000"
    assert contact["chatwoot_contact_id"] == "42"


def test_extract_contact_from_payload_minimal():
    """Verify extraction works when only email is present."""
    payload = {
        "sender": {"id": 7, "email": "minimal@test.com"},
        "conversation": {},
    }
    contact = extract_contact_from_payload(payload)
    assert contact is not None
    assert contact["email"] == "minimal@test.com"
    assert contact["name"] == "Unknown"
    assert contact["phone"] is None


def test_extract_contact_from_payload_empty():
    """Verify None when no useful sender info exists."""
    payload = {"sender": {}, "conversation": {}}
    assert extract_contact_from_payload(payload) is None


def test_extract_contact_from_payload_no_sender():
    """Verify None when sender key is missing entirely."""
    payload = {"conversation": {"id": 5}}
    assert extract_contact_from_payload(payload) is None


@pytest.mark.asyncio
async def test_resolve_or_create_customer_redis_fallback(monkeypatch):
    """Verify customer resolution via Redis fallback when DB is unavailable."""
    db_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.crm.contact_sync.get_redis", lambda: MockRedis())

    # Force DB failure by making get_async_pool raise
    def bad_pool():
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr("core.db.pool.get_async_pool", bad_pool)

    contact = {"name": "Test User", "email": "test@brand.com", "phone": None}
    cid = await resolve_or_create_customer("brand_x", contact)
    assert cid is not None
    assert len(cid) == 36  # UUID format

    # Second call with same email should resolve to same customer
    cid2 = await resolve_or_create_customer("brand_x", contact)
    assert cid2 == cid


@pytest.mark.asyncio
async def test_sync_contact_from_webhook_pipeline(monkeypatch):
    """Verify end-to-end pipeline: extract → resolve → link to session."""
    db_store = {}
    session_store = {}

    class MockRedis:
        async def get(self, key):
            return db_store.get(key)
        async def set(self, key, val):
            db_store[key] = val
            return True
        async def setex(self, key, ttl, val):
            db_store[key] = val
            return True

    monkeypatch.setattr("core.crm.contact_sync.get_redis", lambda: MockRedis())
    monkeypatch.setattr("core.memory.session.get_redis", lambda: MockRedis())

    # Force DB failure to use Redis fallback
    def bad_pool():
        raise RuntimeError("DB unavailable")

    monkeypatch.setattr("core.db.pool.get_async_pool", bad_pool)

    # Mock update_session to track calls
    async def mock_update(session_id, updates):
        session_store[session_id] = updates

    monkeypatch.setattr("core.memory.session.update_session", mock_update)

    payload = {
        "sender": {
            "id": 99,
            "name": "Alice Smith",
            "email": "alice@shop.com",
            "phone_number": "+1234567890",
        },
        "conversation": {"id": 200},
    }

    customer_id = await sync_contact_from_webhook(payload, "brand_z", "sess_abc")
    assert customer_id is not None
    assert session_store["sess_abc"]["customer_id"] == customer_id


@pytest.mark.asyncio
async def test_sync_contact_from_webhook_no_sender(monkeypatch):
    """Verify pipeline returns None gracefully when no contact data is present."""
    payload = {"sender": {}, "conversation": {}}
    result = await sync_contact_from_webhook(payload, "brand_x", "sess_123")
    assert result is None
