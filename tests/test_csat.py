"""Tests for Automated Post-Session CSAT Surveys."""

from __future__ import annotations

import json
import uuid
import pytest
from core.crm.csat import (
    is_csat_pending,
    schedule_csat_survey,
    process_csat_response,
    SURVEY_QUESTION,
    THANK_YOU_RESPONSE,
)


@pytest.mark.asyncio
async def test_schedule_csat_survey(monkeypatch):
    """Verify scheduling of CSAT survey registers in Redis, database, and dispatches message."""
    redis_store = {}
    db_store = {}
    post_calls = []

    class MockRedis:
        async def setex(self, key, ttl, val):
            redis_store[key] = val
            return True

        async def get(self, key):
            return redis_store.get(key)

        async def exists(self, key):
            return 1 if key in redis_store else 0

    async def mock_get_session(sid):
        return {"chatwoot_conv_id": "conv_abc"}

    monkeypatch.setattr("core.crm.csat.get_redis", lambda: MockRedis())
    monkeypatch.setattr("core.crm.csat.get_session", mock_get_session)

    # Mock DB execute
    class MockConn:
        async def execute(self, query, params=None):
            db_store["query"] = query
            db_store["params"] = params

    class MockPool:
        def connection(self):
            class AsyncContext:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return AsyncContext()

    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: MockPool())

    # Mock Chatwoot post message
    def mock_post(conv_id, content, private=False):
        post_calls.append((conv_id, content))
        return True

    monkeypatch.setattr("channels.chat.chatwoot_client.post_message", mock_post)

    session_id = str(uuid.uuid4())
    customer_id = "cust_123"

    scheduled = await schedule_csat_survey(
        session_id=session_id,
        customer_id=customer_id,
        brand_id="boohoo_uk",
        channel="chat",
        intent="return_request",
        outcome="resolved",
        resolution_id="res_abc",
        turn_count=3,
    )

    assert scheduled is True
    assert f"session:csat_pending:{session_id}" in redis_store
    assert redis_store[f"session:csat_pending:{session_id}"] == customer_id

    # Verify DB query was constructed
    assert "INSERT INTO support_sessions" in db_store["query"]
    assert db_store["params"][1] == "boohoo_uk"
    assert db_store["params"][2] == customer_id

    # Verify Chatwoot message dispatch
    assert len(post_calls) == 1
    assert post_calls[0] == ("conv_abc", SURVEY_QUESTION)


@pytest.mark.asyncio
async def test_process_csat_response_valid(monkeypatch):
    """Verify parsing and processing of a valid CSAT response."""
    redis_store = {
        "session:csat_pending:sess_abc": b"cust_123"
    }
    db_store = {}
    post_calls = []
    invalidated_customers = []

    class MockRedis:
        async def get(self, key):
            return redis_store.get(key)

        async def delete(self, key):
            if key in redis_store:
                del redis_store[key]
            return True

    async def mock_get_session(sid):
        return {"chatwoot_conv_id": "conv_abc"}

    monkeypatch.setattr("core.crm.csat.get_redis", lambda: MockRedis())
    monkeypatch.setattr("core.crm.csat.get_session", mock_get_session)

    # Mock DB execute
    class MockConn:
        async def execute(self, query, params=None):
            db_store["query"] = query
            db_store["params"] = params

    class MockPool:
        def connection(self):
            class AsyncContext:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return AsyncContext()

    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: MockPool())

    # Mock other actions
    async def mock_update_sess(sid, updates):
        pass

    async def mock_invalidate(cid):
        invalidated_customers.append(cid)

    monkeypatch.setattr("core.crm.csat.update_session", mock_update_sess)
    monkeypatch.setattr("core.crm.csat.invalidate_customer_cache", mock_invalidate)
    monkeypatch.setattr("channels.chat.chatwoot_client.post_message", lambda *a, **k: post_calls.append(a))

    session_id = str(uuid.uuid4())
    redis_store[f"session:csat_pending:{session_id}"] = b"cust_123"

    processed = await process_csat_response(session_id, "I rate this 4 out of 5!")
    assert processed is True

    # Check pending key was cleared
    assert f"session:csat_pending:{session_id}" not in redis_store

    # Check DB update was executed
    assert "UPDATE support_sessions" in db_store["query"]
    assert db_store["params"][0] == 4.0

    # Check cache invalidation was triggered
    assert invalidated_customers == ["cust_123"]

    # Check thank you response was sent
    assert len(post_calls) == 1
    assert post_calls[0][1] == THANK_YOU_RESPONSE


@pytest.mark.asyncio
async def test_process_csat_response_invalid(monkeypatch):
    """Verify that invalid CSAT responses are ignored."""
    processed = await process_csat_response("sess_abc", "I still need help with my refund!")
    assert processed is False
