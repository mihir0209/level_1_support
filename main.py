"""
FastAPI application — main entrypoint.

Exposes:
  POST /chat          → synchronous chat turn (chat/email channels)
  POST /session/new   → create a new support session
  GET  /session/{id}  → retrieve session state
  GET  /health        → health check (DB connectivity)
  GET  /metrics       → Prometheus metrics

Chatwoot webhook integration:
  POST /webhook/chatwoot  → receives inbound messages from Chatwoot
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from prometheus_client import Counter, Histogram, generate_latest
from pydantic import BaseModel
from starlette.responses import PlainTextResponse

from brands.registry import resolve_brand, BRAND_REGISTRY
from config import settings
from core.memory.session import create_session, get_session, update_session
from core.ontology.models import Channel, Customer, Order, OrderStatus, Shipment
from core.orchestration.graph import get_graph
from core.orchestration.state import L1SupportState
from core.ontology.schema import create_schema, verify_connectivity
from core.ontology.seed_data import seed_all, get_graph_client

log = structlog.get_logger(__name__)

# ── Prometheus metrics ────────────────────────────────────────────────────
MESSAGES_TOTAL = Counter("l1_messages_total", "Total messages processed", ["brand_id", "channel"])
RESOLUTION_LATENCY = Histogram("l1_resolution_latency_seconds", "End-to-end latency per turn")
ESCALATION_TOTAL = Counter("l1_escalations_total", "Total escalations", ["brand_id", "reason"])
SESSIONS_TOTAL = Counter("l1_sessions_total", "Total sessions created", ["brand_id", "channel"])


# ── Lifespan: startup / shutdown ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", environment=settings.environment)

    # Verify graph connectivity and seed data on startup
    if verify_connectivity():
        log.info("falkordb_connected")
        try:
            _, graph = get_graph_client()
            create_schema(graph)
            seed_all(graph)
            log.info("knowledge_graph_ready")
        except Exception as e:
            log.warning("seed_failed", error=str(e))
    else:
        log.warning("falkordb_not_available", msg="Running without knowledge graph — check FalkorDB")

    # Start ingestion scheduler (Phase 2)
    try:
        from ingestion.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        log.warning("scheduler_start_failed", error=str(e))

    yield

    # Shutdown scheduler
    try:
        from ingestion.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass

    log.info("shutdown")


app = FastAPI(
    title="L1 Support AI — E-Commerce Platform",
    description="AI-native L1 customer support for multi-brand e-commerce. "
                "Chat → /chat | Sessions → /session/new | Admin → /admin/overview",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════

class NewSessionRequest(BaseModel):
    brand: str              # brand_id or domain, e.g. "boohoo_uk" or "boohoo.com"
    channel: str = "chat"   # chat | email | voice | whatsapp
    customer_id: str | None = None


class NewSessionResponse(BaseModel):
    session_id: str
    brand_id: str
    brand_name: str
    channel: str


class ChatRequest(BaseModel):
    session_id: str
    message: str
    customer_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    response: str
    intent: str | None = None
    is_resolved: bool = False
    is_escalated: bool = False
    escalation_ticket: str | None = None
    turn_count: int = 0
    latency_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# MOCK CUSTOMER FETCHER (Phase 1 — replace with real API in Phase 2)
# ═══════════════════════════════════════════════════════════════════════════

from datetime import datetime, timezone, timedelta

_MOCK_CUSTOMERS: dict[str, Customer] = {
    "cust_001": Customer(
        id="cust_001", brand_id="boohoo_uk", email="alice@example.com",
        tier=__import__("core.ontology.models", fromlist=["CustomerTier"]).CustomerTier.STANDARD,
        locale="en-GB", total_orders=5,
        recent_orders=[
            Order(
                id="BH-12345-UK", brand_id="boohoo_uk", customer_id="cust_001",
                status=OrderStatus.IN_TRANSIT,
                placed_at=datetime.now(timezone.utc) - timedelta(days=5),
                total_value=45.99, currency="GBP",
                shipment=Shipment(
                    tracking_id="EVRI-987654321", carrier="Evri",
                    estimated_delivery=datetime.now(timezone.utc) + timedelta(days=1),
                    current_status="Out for delivery",
                    tracking_url="https://evri.com/track/EVRI-987654321",
                ),
            )
        ],
    ),
    "cust_002": Customer(
        id="cust_002", brand_id="plt_uk", email="sophie@example.com",
        tier=__import__("core.ontology.models", fromlist=["CustomerTier"]).CustomerTier.LOYALTY,
        locale="en-GB", total_orders=12,
        recent_orders=[
            Order(
                id="PLT-98765-UK", brand_id="plt_uk", customer_id="cust_002",
                status=OrderStatus.PROCESSING,
                placed_at=datetime.now(timezone.utc) - timedelta(hours=2),
                total_value=29.00, currency="GBP",
            )
        ],
    ),
}


def get_mock_customer(customer_id: str | None, brand_id: str) -> Customer | None:
    if not customer_id:
        return None
    return _MOCK_CUSTOMERS.get(customer_id)


# ═══════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/session/new", response_model=NewSessionResponse)
async def new_session(req: NewSessionRequest):
    """Create a new support session. Returns session_id for subsequent chat turns."""
    brand_entry = resolve_brand(req.brand)
    if not brand_entry:
        raise HTTPException(status_code=400, detail=f"Unknown brand: {req.brand}")

    channel = Channel(req.channel.lower())
    session_id = await create_session(
        brand_id=brand_entry.brand_id,
        channel=channel.value,
        customer_id=req.customer_id,
    )

    SESSIONS_TOTAL.labels(brand_id=brand_entry.brand_id, channel=channel.value).inc()
    return NewSessionResponse(
        session_id=session_id,
        brand_id=brand_entry.brand_id,
        brand_name=brand_entry.name,
        channel=channel.value,
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """
    Process a single chat turn.
    Runs the full LangGraph pipeline: context assembly → route → tools → response.
    """
    t_start = time.monotonic()

    # Load session
    session_data = await get_session(req.session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail="Session not found or expired.")

    brand_id = session_data["brand_id"]
    channel = Channel(session_data["channel"])

    # Fetch customer (Phase 1: mock; Phase 2: real API)
    customer = get_mock_customer(
        req.customer_id or session_data.get("customer_id"),
        brand_id,
    )

    # Build LangGraph state — convert stored dicts to HumanMessage objects
    from langchain_core.messages import HumanMessage as HM, AIMessage as AM
    stored = session_data.get("messages", [])
    history_messages = []
    for m in stored:
        if isinstance(m, dict):
            if m.get("role") == "user":
                history_messages.append(HM(content=m["content"]))
            elif m.get("role") == "assistant":
                history_messages.append(AM(content=m["content"]))
        else:
            history_messages.append(m)
    history_messages.append(HM(content=req.message))

    state = L1SupportState(
        session_id=req.session_id,
        brand_id=brand_id,
        channel=channel,
        customer_id=req.customer_id or session_data.get("customer_id"),
        messages=history_messages,
        turn_count=session_data.get("turn_count", 0),
        resolution_attempts=session_data.get("resolution_attempts", 0),
        is_resolved=session_data.get("is_resolved", False),
        customer=customer,
    )

    # Run the graph
    graph = get_graph()
    result_state = await graph.ainvoke(state)

    # Extract AI response from messages — handle both dict and BaseMessage types
    ai_response = ""
    escalation_ticket = None
    for msg in reversed(result_state.get("messages", [])):
        if hasattr(msg, "content") and not hasattr(msg, "tool_call_id"):
            if isinstance(msg, (AIMessage,)):
                ai_response = msg.content
                break
            elif isinstance(msg, dict) and msg.get("role") == "assistant":
                ai_response = msg["content"]
                break

    is_escalated = result_state.get("outcome") == SessionOutcome.ESCALATED
    is_resolved = result_state.get("outcome") == SessionOutcome.RESOLVED

    # Persist updated session state — convert BaseMessage objects to dicts
    updated_messages = [
        *(m.content if isinstance(m, (HM, AM)) else m
          for m in (session_data.get("messages", []) or [])),
        {"role": "user", "content": req.message},
        {"role": "assistant", "content": ai_response},
    ]
    await update_session(req.session_id, {
        "messages": updated_messages,
        "turn_count": result_state.get("turn_count", state.turn_count + 1),
        "resolution_attempts": result_state.get("resolution_attempts", state.resolution_attempts),
        "is_resolved": is_resolved,
        "outcome": result_state.get("outcome").value if result_state.get("outcome") else None,
    })

    latency_ms = (time.monotonic() - t_start) * 1000
    MESSAGES_TOTAL.labels(brand_id=brand_id, channel=channel.value).inc()
    RESOLUTION_LATENCY.observe(latency_ms / 1000)

    intent_id = (
        result_state.get("intent").issue_type
        if result_state.get("intent") else None
    )

    log.info(
        "chat_turn_complete",
        session_id=req.session_id,
        brand_id=brand_id,
        intent=intent_id,
        is_resolved=is_resolved,
        is_escalated=is_escalated,
        latency_ms=round(latency_ms, 1),
    )

    return ChatResponse(
        session_id=req.session_id,
        response=ai_response or "I'm here to help. Could you tell me more about your issue?",
        intent=intent_id,
        is_resolved=is_resolved,
        is_escalated=is_escalated,
        escalation_ticket=escalation_ticket,
        turn_count=result_state.get("turn_count", 1),
        latency_ms=round(latency_ms, 1),
    )


@app.get("/session/{session_id}")
async def get_session_info(session_id: str) -> dict[str, Any]:
    data = await get_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found.")
    # Strip messages for brevity — return metadata only
    return {k: v for k, v in data.items() if k != "messages"}


@app.post("/webhook/chatwoot")
async def chatwoot_webhook(request: Request):
    """
    Receive inbound messages from Chatwoot's webhook.
    Maps Chatwoot conversation to a session and processes the message.
    """
    payload = await request.json()
    event_type = payload.get("event")

    if event_type != "message_created":
        return {"status": "ignored"}

    message_type = payload.get("message_type")
    if message_type != "incoming":
        return {"status": "ignored"}  # only process customer messages

    content = payload.get("content", "").strip()
    conversation = payload.get("conversation", {})
    chatwoot_conv_id = str(conversation.get("id", ""))
    inbox_id = conversation.get("inbox_id")

    if not content or not chatwoot_conv_id:
        return {"status": "skipped"}

    # Resolve brand from inbox_id
    # Phase 1: default to boohoo_uk
    brand_id = "boohoo_uk"

    # Check if session exists for this Chatwoot conversation
    session_key = f"chatwoot:{chatwoot_conv_id}"
    from core.memory.session import get_redis
    r = get_redis()
    session_id = await r.get(session_key)

    if not session_id:
        session_id = await create_session(brand_id=brand_id, channel=Channel.CHAT.value)
        await r.setex(session_key, 7200, session_id)

    # Process message
    chat_req = ChatRequest(session_id=session_id, message=content)
    response = await chat(chat_req)

    log.info(
        "chatwoot_message_processed",
        chatwoot_conv_id=chatwoot_conv_id,
        session_id=session_id,
        intent=response.intent,
    )

    return {"status": "processed", "session_id": session_id, "response": response.response}


@app.get("/health")
async def health():
    graph_ok = verify_connectivity()
    from core.memory.session import get_redis
    try:
        r = get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    status = "healthy" if (graph_ok and redis_ok) else "degraded"
    return {
        "status": status,
        "falkordb": "ok" if graph_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
        "version": "0.1.0",
    }


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN / VISUALIZATION ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

from fastapi import APIRouter
from pydantic import BaseModel

# ── Sub-routers ─────────────────────────────────────────────────────────────
admin_router = APIRouter(prefix="/admin", tags=["Admin"])

# ── Knowledge Graph ─────────────────────────────────────────────────────────
@admin_router.get("/graph/summary")
async def graph_summary():
    """FalkorDB graph overview — node counts by label."""
    try:
        _, graph = get_graph_client()
        result = graph.query("MATCH (n) RETURN labels(n)[0] as label, count(n) as count ORDER BY count DESC")
        nodes = [{"label": r[0], "count": r[1]} for r in result.result_set if r[0]]
        return {"graph": settings.falkordb_host, "total_nodes": sum(n["count"] for n in nodes), "nodes": nodes}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@admin_router.get("/graph/relationships")
async def graph_relationships():
    """All relationship types with counts."""
    try:
        _, graph = get_graph_client()
        result = graph.query(
            "MATCH (a)-[r]->(b) RETURN type(r) as rel_type, count(r) as count ORDER BY count DESC"
        )
        return {"relationships": [{"type": r[0], "count": r[1]} for r in result.result_set]}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@admin_router.get("/graph/brand/{brand_id}")
async def graph_brand(brand_id: str):
    """All nodes connected to a specific brand — by brand_id property."""
    try:
        _, graph = get_graph_client()
        result = graph.query(
            f"MATCH (b:Brand {{brand_id: '{brand_id}'}})-[r]-(n) "
            f"RETURN labels(n)[0] as label, type(r) as rel, count(n) as count "
            f"ORDER BY count DESC"
        )
        return {"brand_id": brand_id, "connections": [{"label": r[0], "relationship": r[1], "count": r[2]} for r in result.result_set]}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@admin_router.get("/graph/issues")
async def graph_issues():
    """Issue types with their resolution paths."""
    try:
        _, graph = get_graph_client()
        result = graph.query(
            "MATCH (it:IssueType)-[r]-(n) "
            "WHERE NOT 'Brand' IN labels(n) "
            "RETURN it.issue_type_id as issue, labels(n)[0] as connected_to, type(r) as rel, count(n) as count"
        )
        return {"issues": [dict(zip(["issue", "connected_to", "relationship", "count"], r)) for r in result.result_set]}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@admin_router.get("/graph/query")
async def graph_query(q: str):
    """Free-text Cypher query against the knowledge graph."""
    try:
        _, graph = get_graph_client()
        result = graph.query(q)
        return {"rows": [list(r) for r in result.result_set], "columns": result.header}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Ontology Schema ────────────────────────────────────────────────────────────
@admin_router.get("/ontology")
async def ontology_overview():
    """High-level ontology: labels, properties, and relationships."""
    try:
        _, graph = get_graph_client()
        labels = graph.query("CALL db.labels()").result_set
        rels = graph.query("CALL db.relationshipTypes()").result_set
        props = graph.query("CALL db.propertyKeys()").result_set
        return {
            "node_labels": sorted([r[0] for r in labels]),
            "relationship_types": sorted([r[0] for r in rels]),
            "property_keys": sorted([r[0] for r in props]),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@admin_router.get("/ontology/brand/{brand_id}")
async def ontology_brand(brand_id: str):
    """Full brand subtree — policies, issue types, FAQs, resolution steps."""
    try:
        _, graph = get_graph_client()
        policies = graph.query(
            f"MATCH (b:Brand {{brand_id: '{brand_id}'}})-[:HAS_POLICY]->(p) "
            f"RETURN p.policy_type as type, p.title as title, keys(p) as fields"
        )
        issues = graph.query(
            f"MATCH (b:Brand {{brand_id: '{brand_id}'}})-[:HAS_ISSUE_TYPE]->(it) "
            f"RETURN it.issue_type_id as issue, it.category as category"
        )
        faqs = graph.query(
            f"MATCH (b:Brand {{brand_id: '{brand_id}'}})-[:HAS_FAQ]->(f) "
            f"RETURN f.question as question, f.category as category LIMIT 5"
        )
        return {
            "brand_id": brand_id,
            "policies": [dict(zip(["type", "title", "fields"], r)) for r in policies.result_set],
            "issue_types": [dict(zip(["issue", "category"], r)) for r in issues.result_set],
            "sample_faqs": [dict(zip(["question", "category"], r)) for r in faqs.result_set],
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Knowledge Base ──────────────────────────────────────────────────────────
@admin_router.get("/kb/faqs")
async def kb_faqs(brand_id: str | None = None, category: str | None = None):
    """FAQ content from FalkorDB — optionally filtered by brand and category."""
    try:
        _, graph = get_graph_client()
        where = "WHERE 1=1"
        if brand_id:
            where += f" AND f.brand_id = '{brand_id}'"
        if category:
            where += f" AND f.category = '{category}'"
        result = graph.query(
            f"MATCH (f:FAQ) {where} "
            f"RETURN f.question as question, f.answer as answer, f.category as category, f.keywords as keywords "
            f"ORDER BY f.category, f.question LIMIT 100"
        )
        return {"count": len(result.result_set), "faqs": [dict(zip(["question", "answer", "category", "keywords"], r)) for r in result.result_set]}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@admin_router.get("/kb/policies")
async def kb_policies(brand_id: str | None = None):
    """Policy summaries from FalkorDB."""
    try:
        _, graph = get_graph_client()
        where = f"WHERE p.brand_id = '{brand_id}'" if brand_id else ""
        result = graph.query(
            f"MATCH (p:Policy) {where} "
            f"RETURN p.policy_type as type, p.title as title, p.brand_id as brand_id"
        )
        return {"policies": [dict(zip(["type", "title", "brand_id"], r)) for r in result.result_set]}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@admin_router.get("/kb/pgvector/chunks")
async def kb_chunks(brand_id: str | None = None, chunk_type: str | None = None, limit: int = 20):
    """Knowledge chunks from Postgres pgvector — semantic search base."""
    import psycopg2
    conn = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )
    try:
        cur = conn.cursor()
        query = "SELECT id, brand_id, chunk_type, left(content, 200) as content_preview, metadata FROM knowledge_chunks WHERE 1=1"
        if brand_id:
            query += f" AND brand_id = '{brand_id}'"
        if chunk_type:
            query += f" AND chunk_type = '{chunk_type}'"
        query += f" LIMIT {limit}"
        cur.execute(query)
        rows = cur.fetchall()
        return {"count": len(rows), "chunks": [{"id": str(r[0]), "brand_id": r[1], "chunk_type": r[2], "preview": r[3], "metadata": r[4]} for r in rows]}
    finally:
        conn.close()


# ── Sessions ────────────────────────────────────────────────────────────────
@admin_router.get("/sessions")
async def list_sessions(brand_id: str | None = None, limit: int = 50):
    """All active sessions from Redis — optionally filtered by brand."""
    from core.memory.session import get_redis
    r = get_redis()
    session_keys = []
    async for key in r.scan_iter(match="session:*"):
        session_keys.append(key)
    sessions = []
    for key in session_keys[:limit]:
        data = await r.get(key)
        if data:
            import json
            d = json.loads(data)
            if brand_id is None or d.get("brand_id") == brand_id:
                sessions.append({
                    "session_id": d.get("session_id"),
                    "brand_id": d.get("brand_id"),
                    "channel": d.get("channel"),
                    "turn_count": d.get("turn_count", 0),
                    "outcome": d.get("outcome"),
                    "created_at": d.get("created_at"),
                })
    return {"count": len(sessions), "sessions": sessions}


@admin_router.get("/sessions/{session_id}")
async def get_session_detail(session_id: str):
    """Full session state including message history."""
    data = await get_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


# ── Brand Registry ──────────────────────────────────────────────────────────
@admin_router.get("/brands")
async def list_brands():
    """All registered brands in the registry."""
    from brands.registry import BRAND_REGISTRY
    return {
        "count": len(BRAND_REGISTRY),
        "brands": [
            {"brand_id": b.brand_id, "name": b.name, "domains": b.domains, "locale": b.default_region, "chatwoot_inbox": b.chatwoot_inbox_id}
            for b in BRAND_REGISTRY
        ],
    }


@admin_router.get("/brands/{brand_id}")
async def get_brand_detail(brand_id: str):
    """Single brand entry."""
    from brands.registry import get_brand_by_id
    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Brand not found")
    return {"brand_id": entry.brand_id, "name": entry.name, "domains": entry.domains, "locale": entry.default_region, "chatwoot_inbox": entry.chatwoot_inbox_id}


# ── System Overview ─────────────────────────────────────────────────────────
@admin_router.get("/overview")
async def system_overview():
    """Full system snapshot for dashboard/demo — all stores at a glance."""
    from core.memory.session import get_redis

    # Graph stats
    graph_ok, graph_stats = False, {}
    try:
        _, graph = get_graph_client()
        labels = graph.query("MATCH (n) RETURN labels(n)[0] as label, count(n) as c ORDER BY c DESC").result_set
        rels = graph.query("MATCH ()-[r]->() RETURN type(r) as t, count(r) as c ORDER BY c DESC").result_set
        graph_ok = True
        graph_stats = {
            "nodes_by_label": [{"label": r[0], "count": r[1]} for r in labels],
            "relationships": [{"type": r[0], "count": r[1]} for r in rels],
        }
    except Exception:
        pass

    # Redis sessions
    session_count = 0
    try:
        r = get_redis()
        async for _ in r.scan_iter(match="session:*"):
            session_count += 1
    except Exception:
        pass

    # Postgres KB
    pg_stats = {}
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=settings.postgres_host, port=settings.postgres_port,
            dbname=settings.postgres_db, user=settings.postgres_user,
            password=settings.postgres_password,
        )
        try:
            cur = conn.cursor()
            cur.execute("SELECT brand_id, count(*) FROM knowledge_chunks GROUP BY brand_id")
            pg_stats = dict(cur.fetchall())
        finally:
            conn.close()
    except Exception:
        pass

    return {
        "services": {
            "api": {"url": f"http://localhost:{settings.app_port}", "status": "up"},
            "falkordb": {"host": settings.falkordb_host, "port": settings.falkordb_port, "status": "up" if graph_ok else "down"},
            "postgres": {"host": settings.postgres_host, "port": settings.postgres_port, "db": settings.postgres_db},
            "redis": {"url": settings.redis_url, "status": "up"},
            "chatwoot": {"url": "http://localhost:3000"},
            "n8n": {"url": "http://localhost:5678"},
            "grafana": {"url": "http://localhost:3001"},
        },
        "knowledge_graph": graph_stats,
        "postgres_kb": pg_stats,
        "active_sessions": session_count,
        "registered_brands": len(BRAND_REGISTRY),
    }


# Register admin router
app.include_router(admin_router)


# ═══════════════════════════════════════════════════════════════════════════
# VOICE ENDPOINTS (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════

class VoiceStartRequest(BaseModel):
    brand: str
    customer_name: str | None = None


@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    """
    Twilio webhook — receives inbound call and returns TwiML to connect to Daily.
    Twilio sends form-encoded POST; brand is resolved from the Twilio number's config.
    """
    form = await request.form()
    from_number = form.get("From", "")
    call_sid = form.get("CallSid", str(uuid.uuid4()))
    to_number = form.get("To", "")

    # Resolve brand from the Twilio number being called
    # Phase 2: map TWILIO_PHONE_NUMBER → brand via config
    brand_id = "boohoo_uk"  # default; extend via per-number config

    from channels.voice.router import handle_incoming_call
    twiml = await handle_incoming_call(
        brand_id=brand_id,
        from_number=from_number,
        call_sid=call_sid,
    )
    return PlainTextResponse(twiml, media_type="text/xml")


@app.post("/voice/start")
async def voice_start(req: VoiceStartRequest):
    """Dev endpoint — start a voice call session and return the Daily room URL to join."""
    brand_entry = resolve_brand(req.brand)
    if not brand_entry:
        raise HTTPException(status_code=400, detail=f"Unknown brand: {req.brand}")

    from channels.voice.router import start_dev_call
    call_info = await start_dev_call(brand_id=brand_entry.brand_id)
    return call_info


@app.get("/voice/active")
async def voice_active():
    """List all active voice calls (admin)."""
    from channels.voice.router import get_active_calls
    return {"calls": get_active_calls()}


# ═══════════════════════════════════════════════════════════════════════════
# INGESTION ENDPOINTS (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════

class IngestRequest(BaseModel):
    brand: str
    url: str | None = None  # if None: full brand crawl


@app.post("/admin/ingest")
async def admin_ingest(req: IngestRequest):
    """
    Trigger a crawl-to-graph ingestion run.
    - With url: ingest a single page immediately (on-demand)
    - Without url: trigger a full brand crawl in background
    """
    brand_entry = resolve_brand(req.brand)
    if not brand_entry:
        raise HTTPException(status_code=400, detail=f"Unknown brand: {req.brand}")

    brand_id = brand_entry.brand_id
    base_url = f"https://{brand_entry.domains[0]}"

    if req.url:
        from ingestion.pipeline import run_url
        report = await run_url(brand_id=brand_id, url=req.url)
        return report.to_dict()
    else:
        from ingestion.scheduler import trigger_crawl_now
        return await trigger_crawl_now(brand_id)


@app.get("/admin/jobs")
async def admin_jobs():
    """List all scheduled ingestion jobs."""
    from ingestion.scheduler import list_jobs
    return {"jobs": list_jobs()}


# ── Import SessionOutcome for the chat endpoint ────────────────────────────
from core.ontology.models import SessionOutcome  # noqa: E402 — needed after app definition


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, log_level=settings.log_level.lower())
