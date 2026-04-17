"""
API integration tests using FastAPI TestClient — no running server required.

Tests every endpoint with mock Redis and mock graph (no external services).
Verifies:
  - Session lifecycle (create, get, update)
  - Chat turns with mock LLM
  - Chatwoot webhook flow
  - Health check response
  - Voice endpoints
  - Admin ingestion endpoints
  - Error paths (404, 400)
"""

import os
import sys
import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fixtures: mock external services ─────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    """In-memory Redis substitute for all session operations."""
    store = {}

    class FakeRedis:
        async def get(self, key):
            return store.get(key)

        async def setex(self, key, ttl, value):
            store[key] = value

        async def delete(self, key):
            store.pop(key, None)

        async def expire(self, key, ttl):
            pass

        async def ping(self):
            return True

    fake = FakeRedis()
    import core.memory.session as sess
    monkeypatch.setattr(sess, "get_redis", lambda: fake)
    # Also patch in main (it imports get_redis directly)
    import main as m
    monkeypatch.setattr("core.memory.session.get_redis", lambda: fake)
    return store


@pytest.fixture(autouse=True)
def mock_falkordb(monkeypatch):
    """Prevent FalkorDB connection attempts."""
    import core.ontology.schema as schema
    monkeypatch.setattr(schema, "verify_connectivity", lambda: False)

    # Also mock seed_all/create_schema so lifespan doesn't fail
    import core.ontology.seed_data as seed
    monkeypatch.setattr(seed, "seed_all", lambda g: None)
    monkeypatch.setattr(schema, "create_schema", lambda g: None)


@pytest.fixture(autouse=True)
def mock_scheduler(monkeypatch):
    """Prevent APScheduler from starting during tests."""
    import ingestion.scheduler as sched
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(sched, "stop_scheduler", lambda: None)


@pytest.fixture
def client(mock_redis, mock_falkordb, mock_scheduler):
    from fastapi.testclient import TestClient
    import main  # import after patching
    with TestClient(main.app) as c:
        yield c


# ═══════════════════════════════════════════════════════════════════════════
# Health endpoint
# ═══════════════════════════════════════════════════════════════════════════

class TestHealth:

    def test_health_returns_status(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert "status" in body
        assert body["status"] in ("healthy", "degraded")
        assert "falkordb" in body
        assert "redis" in body
        assert body["version"] == "0.1.0"

    def test_health_falkordb_unavailable_gives_degraded(self, client):
        r = client.get("/health")
        body = r.json()
        # FalkorDB is mocked as unavailable
        assert body["falkordb"] == "unavailable"


# ═══════════════════════════════════════════════════════════════════════════
# Session endpoints
# ═══════════════════════════════════════════════════════════════════════════

class TestSession:

    def test_new_session_valid_brand(self, client):
        r = client.post("/session/new", json={"brand": "boohoo_uk", "channel": "chat"})
        assert r.status_code == 200
        body = r.json()
        assert "session_id" in body
        assert len(body["session_id"]) == 36  # UUID
        assert body["brand_id"] == "boohoo_uk"
        assert body["channel"] == "chat"

    def test_new_session_resolve_by_domain(self, client):
        r = client.post("/session/new", json={"brand": "boohoo.com", "channel": "chat"})
        assert r.status_code == 200
        body = r.json()
        assert body["brand_id"] == "boohoo_uk"

    def test_new_session_unknown_brand(self, client):
        r = client.post("/session/new", json={"brand": "unknownbrand123", "channel": "chat"})
        assert r.status_code == 400

    def test_new_session_with_customer_id(self, client):
        r = client.post("/session/new", json={
            "brand": "plt_uk", "channel": "chat", "customer_id": "cust_001"
        })
        assert r.status_code == 200
        body = r.json()
        assert body["brand_id"] == "plt_uk"

    def test_new_session_all_channels(self, client):
        for channel in ["chat", "email", "voice", "whatsapp"]:
            r = client.post("/session/new", json={"brand": "boohoo_uk", "channel": channel})
            assert r.status_code == 200, f"Failed for channel: {channel}"
            assert r.json()["channel"] == channel

    def test_get_session_found(self, client):
        # Create first
        r = client.post("/session/new", json={"brand": "boohoo_uk", "channel": "chat"})
        session_id = r.json()["session_id"]
        # Then fetch
        r2 = client.get(f"/session/{session_id}")
        assert r2.status_code == 200
        body = r2.json()
        assert body["session_id"] == session_id
        assert body["brand_id"] == "boohoo_uk"
        # Messages stripped from GET response
        assert "messages" not in body

    def test_get_session_not_found(self, client):
        r = client.get("/session/nonexistent-session-id")
        assert r.status_code == 404

    def test_all_13_brands_create_sessions(self, client):
        from brands.registry import BRAND_REGISTRY
        for entry in BRAND_REGISTRY:
            r = client.post("/session/new", json={"brand": entry.brand_id, "channel": "chat"})
            assert r.status_code == 200, f"Failed for brand: {entry.brand_id}"
            assert r.json()["brand_id"] == entry.brand_id


# ═══════════════════════════════════════════════════════════════════════════
# Chat endpoint (mocked LangGraph)
# ═══════════════════════════════════════════════════════════════════════════

class TestChat:

    @pytest.fixture
    def mock_graph(self, monkeypatch):
        """Mock the LangGraph pipeline to return a canned response."""
        from langchain_core.messages import AIMessage
        from core.ontology.models import SessionOutcome, IntentResult, IssueCategory

        mock_result = {
            "messages": [AIMessage(content="Your order BH-12345 is currently in transit with Evri. Expected delivery tomorrow.")],
            "intent": IntentResult(issue_type="wismo", category=IssueCategory.DELIVERY, confidence=0.95, entities={}),
            "turn_count": 1,
            "resolution_attempts": 1,
            "is_resolved": False,
            "outcome": None,
        }

        async def fake_ainvoke(state, *args, **kwargs):
            return mock_result

        mock_g = MagicMock()
        mock_g.ainvoke = fake_ainvoke

        import core.orchestration.graph as graph_mod
        monkeypatch.setattr(graph_mod, "get_graph", lambda: mock_g)
        import main
        monkeypatch.setattr(main, "get_graph", lambda: mock_g)
        return mock_result

    def test_chat_basic_turn(self, client, mock_graph):
        # Create session
        r = client.post("/session/new", json={"brand": "boohoo_uk", "channel": "chat"})
        session_id = r.json()["session_id"]

        r = client.post("/chat", json={
            "session_id": session_id,
            "message": "Where is my order BH-12345?",
            "customer_id": "cust_001",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == session_id
        assert "order" in body["response"].lower() or "in transit" in body["response"].lower()
        assert body["intent"] == "wismo"
        assert body["turn_count"] == 1
        assert body["latency_ms"] > 0

    def test_chat_session_not_found(self, client, mock_graph):
        r = client.post("/chat", json={
            "session_id": "nonexistent-session",
            "message": "Hello",
        })
        assert r.status_code == 404

    def test_chat_resolved_session(self, client, monkeypatch):
        """Session marked resolved → is_resolved True in response."""
        from langchain_core.messages import AIMessage
        from core.ontology.models import SessionOutcome, IntentResult, IssueCategory

        mock_result = {
            "messages": [AIMessage(content="Great! Your return label has been emailed. Refund in 5-7 days.")],
            "intent": IntentResult(issue_type="return_initiate", category=IssueCategory.RETURN, confidence=0.91, entities={}),
            "turn_count": 2,
            "resolution_attempts": 1,
            "is_resolved": True,
            "outcome": SessionOutcome.RESOLVED,
        }

        async def fake_ainvoke(state, *args, **kwargs):
            return mock_result

        mock_g = MagicMock()
        mock_g.ainvoke = fake_ainvoke
        import main
        monkeypatch.setattr(main, "get_graph", lambda: mock_g)

        r = client.post("/session/new", json={"brand": "boohoo_uk", "channel": "chat"})
        session_id = r.json()["session_id"]
        r = client.post("/chat", json={"session_id": session_id, "message": "I want to return my dress."})
        assert r.status_code == 200
        body = r.json()
        assert body["is_resolved"] is True

    def test_chat_escalated_session(self, client, monkeypatch):
        """Session escalated → is_escalated True in response."""
        from langchain_core.messages import AIMessage
        from core.ontology.models import SessionOutcome, IntentResult, IssueCategory

        mock_result = {
            "messages": [AIMessage(content="I'm connecting you with a specialist who can help.")],
            "intent": IntentResult(issue_type="escalate", category=IssueCategory.ORDER, confidence=0.85, entities={}),
            "turn_count": 5,
            "resolution_attempts": 3,
            "is_resolved": False,
            "outcome": SessionOutcome.ESCALATED,
        }

        async def fake_ainvoke(state, *args, **kwargs):
            return mock_result

        mock_g = MagicMock()
        mock_g.ainvoke = fake_ainvoke
        import main
        monkeypatch.setattr(main, "get_graph", lambda: mock_g)

        r = client.post("/session/new", json={"brand": "boohoo_uk", "channel": "chat"})
        session_id = r.json()["session_id"]
        r = client.post("/chat", json={"session_id": session_id, "message": "This is unacceptable!"})
        assert r.status_code == 200
        assert r.json()["is_escalated"] is True

    def test_chat_persists_messages(self, client, mock_graph, mock_redis):
        """After a chat turn, session in Redis should have updated messages."""
        r = client.post("/session/new", json={"brand": "boohoo_uk", "channel": "chat"})
        session_id = r.json()["session_id"]
        client.post("/chat", json={"session_id": session_id, "message": "Where is my order?"})

        # Fetch session — turn_count should be updated
        r2 = client.get(f"/session/{session_id}")
        assert r2.status_code == 200
        body = r2.json()
        assert body.get("turn_count", 0) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Chatwoot webhook
# ═══════════════════════════════════════════════════════════════════════════

class TestChatwootWebhook:

    @pytest.fixture
    def mock_graph_for_webhook(self, monkeypatch):
        from langchain_core.messages import AIMessage
        from core.ontology.models import SessionOutcome, IntentResult, IssueCategory

        mock_result = {
            "messages": [AIMessage(content="Thank you for contacting us. How can I help?")],
            "intent": IntentResult(issue_type="wismo", category=IssueCategory.DELIVERY, confidence=0.80, entities={}),
            "turn_count": 1,
            "resolution_attempts": 0,
            "is_resolved": False,
            "outcome": None,
        }

        async def fake_ainvoke(state, *args, **kwargs):
            return mock_result

        mock_g = MagicMock()
        mock_g.ainvoke = fake_ainvoke
        import main
        monkeypatch.setattr(main, "get_graph", lambda: mock_g)

    def test_webhook_message_created(self, client, mock_graph_for_webhook):
        payload = {
            "event": "message_created",
            "message_type": "incoming",
            "content": "Hi, where is my order?",
            "conversation": {"id": "555", "inbox_id": 1},
        }
        r = client.post("/webhook/chatwoot", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "processed"
        assert "session_id" in body
        assert "response" in body

    def test_webhook_ignores_outgoing(self, client):
        payload = {
            "event": "message_created",
            "message_type": "outgoing",
            "content": "Bot reply",
            "conversation": {"id": "556"},
        }
        r = client.post("/webhook/chatwoot", json=payload)
        assert r.json()["status"] == "ignored"

    def test_webhook_ignores_other_events(self, client):
        r = client.post("/webhook/chatwoot", json={"event": "conversation_updated"})
        assert r.json()["status"] == "ignored"

    def test_webhook_skips_empty_content(self, client):
        payload = {
            "event": "message_created",
            "message_type": "incoming",
            "content": "",
            "conversation": {"id": "557"},
        }
        r = client.post("/webhook/chatwoot", json=payload)
        assert r.json()["status"] == "skipped"

    def test_webhook_same_conversation_reuses_session(self, client, mock_graph_for_webhook):
        """Same Chatwoot conversation ID should reuse the same session."""
        payload = {
            "event": "message_created",
            "message_type": "incoming",
            "content": "First message",
            "conversation": {"id": "999", "inbox_id": 1},
        }
        r1 = client.post("/webhook/chatwoot", json=payload)
        session_1 = r1.json()["session_id"]

        payload["content"] = "Second message"
        r2 = client.post("/webhook/chatwoot", json=payload)
        session_2 = r2.json()["session_id"]

        assert session_1 == session_2  # same session reused


# ═══════════════════════════════════════════════════════════════════════════
# Voice endpoints
# ═══════════════════════════════════════════════════════════════════════════

class TestVoiceEndpoints:

    @pytest.fixture(autouse=True)
    def mock_voice_router(self, monkeypatch):
        """Mock Daily API calls and Pipecat pipeline."""
        import channels.voice.router as router
        monkeypatch.setattr(router, "_create_daily_room", AsyncMock(return_value={
            "room_url": "https://test.daily.co/l1-boohoo-abc12345",
            "room_name": "l1-boohoo-abc12345",
            "token": "mock_daily_token",
        }))
        monkeypatch.setattr(router, "_run_pipeline_bg", AsyncMock())

    def test_voice_start_valid_brand(self, client):
        r = client.post("/voice/start", json={"brand": "boohoo_uk"})
        assert r.status_code == 200
        body = r.json()
        assert "session_id" in body
        assert "room_url" in body
        assert "call_sid" in body
        assert "join_url" in body

    def test_voice_start_unknown_brand(self, client):
        r = client.post("/voice/start", json={"brand": "unknownbrand"})
        assert r.status_code == 400

    def test_voice_incoming_twilio(self, client):
        r = client.post("/voice/incoming", data={
            "From": "+447700900123",
            "To": "+442012345678",
            "CallSid": "CA123abc",
        })
        assert r.status_code == 200
        assert "<?xml" in r.text
        assert "<Dial>" in r.text

    def test_voice_active_list(self, client):
        r = client.get("/voice/active")
        assert r.status_code == 200
        assert "calls" in r.json()


# ═══════════════════════════════════════════════════════════════════════════
# Admin ingestion endpoints
# ═══════════════════════════════════════════════════════════════════════════

class TestAdminEndpoints:

    def test_admin_jobs_list(self, client):
        r = client.get("/admin/jobs")
        assert r.status_code == 200
        assert "jobs" in r.json()

    def test_admin_ingest_unknown_brand(self, client):
        r = client.post("/admin/ingest", json={"brand": "fakebrand999"})
        assert r.status_code == 400

    def test_admin_ingest_triggers_background(self, client, monkeypatch):
        import ingestion.scheduler as sched
        triggered = []
        async def fake_trigger(brand_id):
            triggered.append(brand_id)
            return {"status": "triggered", "brand_id": brand_id, "triggered_at": "2026-01-01"}
        monkeypatch.setattr(sched, "trigger_crawl_now", fake_trigger)

        r = client.post("/admin/ingest", json={"brand": "boohoo_uk"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "triggered"
        assert "boohoo_uk" in triggered

    def test_admin_ingest_single_url(self, client, monkeypatch):
        from ingestion.pipeline import PipelineReport
        mock_report = PipelineReport(brand_id="boohoo_uk")
        mock_report.pages_crawled = 1
        mock_report.auto_approved = 3
        mock_report.finish()

        import ingestion.pipeline as pipeline_mod
        monkeypatch.setattr(pipeline_mod, "run_url", AsyncMock(return_value=mock_report))

        r = client.post("/admin/ingest", json={
            "brand": "boohoo_uk",
            "url": "https://boohoo.com/help/returns",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["brand_id"] == "boohoo_uk"
        assert body["pages_crawled"] == 1
        assert body["auto_approved"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# Metrics endpoint
# ═══════════════════════════════════════════════════════════════════════════

class TestMetrics:

    def test_metrics_prometheus_format(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        # Prometheus text format always starts with # HELP or has metric lines
        assert len(r.text) > 0
