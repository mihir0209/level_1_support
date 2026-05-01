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

import asyncio
import random
import time
import uuid
import warnings
from contextlib import asynccontextmanager
from typing import Any

# Silence litellm/pydantic serializer noise caused by Azure OpenAI response
# shapes not matching litellm's internal Pydantic model field counts exactly.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings", category=UserWarning)
# Silence the duckduckgo-search → ddgs rename warning (handled in agent_collector)
warnings.filterwarnings("ignore", message=".*renamed to.*ddgs.*", category=RuntimeWarning)

import structlog
from typing import Annotated
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Header, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from prometheus_client import Counter, Histogram, generate_latest

from brands.registry import resolve_brand, BRAND_REGISTRY
from config import settings
from core.memory.session import create_session, get_session, update_session
from core.ontology.models import Channel, Customer, CustomerTier, Order, OrderItem, OrderStatus, Shipment
from core.orchestration.graph import get_graph
from core.orchestration.state import L1SupportState
from core.ontology.schema import create_schema, verify_connectivity
from core.ontology.seed_data import seed_all_with_kb as seed_all, get_graph_client

log = structlog.get_logger(__name__)

# ── Prometheus metrics ────────────────────────────────────────────────────
MESSAGES_TOTAL = Counter("l1_messages_total", "Total messages processed", ["brand_id", "channel"])
RESOLUTION_LATENCY = Histogram("l1_resolution_latency_seconds", "End-to-end latency per turn")
ESCALATION_TOTAL = Counter("l1_escalations_total", "Total escalations", ["brand_id", "reason"])
SESSIONS_TOTAL = Counter("l1_sessions_total", "Total sessions created", ["brand_id", "channel"])


# ── Lifespan: startup / shutdown ──────────────────────────────────────────

from contextlib import asynccontextmanager

# Track graceful shutdown flag
_shutdown_requested = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _shutdown_requested
    _shutdown_requested = False

    # Configure structured logging before any log calls
    from core.logging_config import configure_logging
    configure_logging(settings.log_level, settings.environment)

    # Initialize OpenTelemetry tracing
    from core.tracing import get_tracer
    tracer = get_tracer()
    log.info("startup", environment=settings.environment, otel_service="l1_support")

    # Restore dynamically registered brands from Redis
    try:
        import json as _j
        from core.memory.session import get_redis
        from brands.registry import BrandEntry, register_brand
        _r = get_redis()
        stored = await _r.hgetall("brand_registry")
        for _raw in stored.values():
            try:
                _d = _j.loads(_raw)
                register_brand(BrandEntry(**_d))
            except Exception:
                pass
        if stored:
            log.info("dynamic_brands_restored", count=len(stored))
    except Exception as e:
        log.warning("dynamic_brands_restore_failed", error=str(e))

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

    # Start feedback consumer (async queue aggregator)
    try:
        from feedback.consumer import start_consumer
        start_consumer()
    except Exception as e:
        log.warning("feedback_consumer_start_failed", error=str(e))

    # Register demo tools API
    from core.tools.api import router as demo_router
    app.include_router(demo_router)

    yield

    # Graceful shutdown — drain in-flight requests before exiting
    log.info("shutdown_initiated")
    _shutdown_requested = True

    # Shutdown scheduler
    try:
        from ingestion.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass

    # Stop feedback consumer
    try:
        from feedback.consumer import stop_consumer
        stop_consumer()
    except Exception:
        pass

    log.info("shutdown")


_OPENAPI_TAGS = [
    {"name": "Chat",          "description": "Core chat/session lifecycle — start a session, send a message, get session state."},
    {"name": "Graph",         "description": "FalkorDB knowledge graph — node counts, relationship types, brand subgraphs, raw Cypher queries."},
    {"name": "Knowledge Base","description": "FAQs, policies, and pgvector semantic-search chunks stored across graph + Postgres."},
    {"name": "Brands",        "description": "Brand registry — all 13 Boohoo-group brands with their `brand_id`, domains, and locales."},
    {"name": "Operations",    "description": "CRM operations — customers, orders, returns, promos. **Auth:** `X-API-KEY` header. Default key: `l1support-demo-key-2026` (override via `ADMIN_API_KEY` env var)."},
    {"name": "Sessions",      "description": "Active support sessions stored in Redis — list, inspect, filter by brand."},
    {"name": "System",        "description": "Health check, Prometheus metrics, full system snapshot, scheduled ingestion jobs."},
    {"name": "Integrations",  "description": "External tool connections — register company-provided MCP server URLs, REST API base URLs, and auth headers. Registered tools are injected into the agent at runtime."},
    {"name": "Voice",         "description": "Twilio/Daily voice channel — inbound calls, dev room start, active call list."},
]

app = FastAPI(
    title="L1 Support AI — E-Commerce Platform",
    description=(
        "AI-native L1 customer support for multi-brand e-commerce.\n\n"
        "**Quick start:**\n"
        "1. `POST /session/new` with a `brand` (e.g. `boohoo_uk`) → get `session_id`\n"
        "2. `POST /chat` with `session_id` + `message`\n\n"
        "**Operations endpoints** require `X-API-KEY` header. "
        "Default value: `l1support-demo-key-2026` (set `ADMIN_API_KEY` env var to change).\n\n"
        "**Visual dashboard:** [`/admin/ui`](/admin/ui)"
    ),
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=_OPENAPI_TAGS,
)

from core.middleware.rate_limit import rate_limit_middleware
from core.logging_config import bind_correlation, clear_correlation

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(rate_limit_middleware)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    """
    Inject request_id from X-Request-ID header or generate one.
    Bind to structlog correlation context for all log entries in this request.
    """
    import uuid
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:16]
    brand_id = request.query_params.get("brand_id") or ""
    session_id = request.headers.get("X-Session-ID") or ""
    bind_correlation(request_id=request_id, brand_id=brand_id, session_id=session_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    clear_correlation()
    return response

# Serve the admin visualizer at /admin/ui
import os as _os
_static_dir = _os.path.join(_os.path.dirname(__file__), "static")
_admin_dir = _os.path.join(_static_dir, "admin")
if _os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
if _os.path.isdir(_admin_dir):
    app.mount("/admin/static", StaticFiles(directory=_admin_dir), name="admin_static")


@app.get("/", include_in_schema=False)
async def root():
    """Redirect root → /admin/ui (the main operator dashboard)."""
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/admin/ui", status_code=302)


@app.get("/admin/ui", include_in_schema=False)
async def admin_ui():
    """L1 Support AI — main operator dashboard (support demo, sessions, monitoring, admin)."""
    return FileResponse(_os.path.join(_static_dir, "admin", "index.html"))


@app.get("/admin/ui/{tab}", include_in_schema=False)
async def admin_ui_tab(tab: str):
    """Tab-specific URLs like /admin/ui/ontology, /admin/ui/sessions, etc."""
    return FileResponse(_os.path.join(_static_dir, "admin", "index.html"))


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


# ── Dynamic demo data store (populated per-brand via POST /admin/brands/{id}/demo-data) ──
_DEMO_DATA: dict[str, dict] = {}


def _generate_brand_demo_data(brand_id: str, brand_name: str) -> dict:
    """Generate realistic mock customers / orders / returns / promos for a brand."""
    now = datetime.now(timezone.utc)
    prefix = brand_id[:3].upper()
    currency = "USD" if any(x in brand_id for x in ["us", "usa"]) else "GBP"
    carriers = ["Evri", "Royal Mail", "DPD", "Yodel", "DHL"]
    carrier = random.choice(carriers)

    def _order(cid, suffix, status, days_ago, value, items, *, days_ahead=2):
        o = Order(
            id=f"{prefix}-{suffix}", brand_id=brand_id, customer_id=cid,
            status=status, placed_at=now - timedelta(days=days_ago),
            total_value=value, currency=currency, items=items,
        )
        if status in (OrderStatus.IN_TRANSIT, OrderStatus.DISPATCHED):
            o.shipment = Shipment(
                tracking_id=f"{carrier.upper()[:4]}-{random.randint(100000000, 999999999)}",
                carrier=carrier,
                estimated_delivery=now + timedelta(days=days_ahead),
                current_status="Out for delivery" if days_ahead <= 1 else "In transit",
                tracking_url=f"https://www.{carrier.lower().replace(' ', '')}.com/track",
            )
        return o

    def _items(*args):  # (sku, name, qty, price, size, color)
        return [OrderItem(sku=a[0], name=a[1], quantity=a[2], price_paid=a[3], size=a[4], color=a[5]) for a in args]

    # ── 6 customers ────────────────────────────────────────────────────────
    customers: dict[str, Customer] = {}

    # 1. Standard — order in transit
    c1 = f"{brand_id}_cust_001"
    customers[c1] = Customer(
        id=c1, brand_id=brand_id, email="emma.jones@example.com",
        tier=CustomerTier.STANDARD, locale="en-GB", total_orders=3,
        recent_orders=[
            _order(c1, "48291", OrderStatus.IN_TRANSIT, 3, 67.99,
                   _items(("TOP-RIB-BLK-12","Ribbed Knit Top",1,22.99,"12","Black"),
                          ("JNS-HW-BLU-10","High Waist Straight Jeans",1,45.00,"10","Blue"))),
            _order(c1, "41204", OrderStatus.DELIVERED, 18, 34.50,
                   _items(("DRS-FLR-GRN-08","Floral Midi Dress",1,34.50,"8","Green"))),
        ],
    )

    # 2. Standard — processing (just placed)
    c2 = f"{brand_id}_cust_002"
    customers[c2] = Customer(
        id=c2, brand_id=brand_id, email="priya.sharma@example.com",
        tier=CustomerTier.STANDARD, locale="en-GB", total_orders=1,
        recent_orders=[
            _order(c2, "52017", OrderStatus.PROCESSING, 0, 89.00,
                   _items(("CTN-STS-WHT-10","Cotton Striped Set",1,55.00,"10","White"),
                          ("SNK-WHT-06","White Trainers",1,34.00,"6",None))),
        ],
    )

    # 3. Loyalty — return pending
    c3 = f"{brand_id}_cust_003"
    customers[c3] = Customer(
        id=c3, brand_id=brand_id, email="sophie.williams@example.com",
        tier=CustomerTier.LOYALTY, locale="en-GB", total_orders=9,
        recent_orders=[
            _order(c3, "39854", OrderStatus.RETURN_PENDING, 8, 58.00,
                   _items(("BLZ-OVR-CRM-12","Oversized Blazer",1,58.00,"12","Cream"))),
            _order(c3, "37201", OrderStatus.DELIVERED, 25, 112.50,
                   _items(("SUIT-PWR-NVY-10","Power Suit Set",1,112.50,"10","Navy"))),
        ],
        open_returns=[{
            "return_id": f"RET-{prefix}-39854",
            "order_id": f"{prefix}-39854",
            "item": "Oversized Blazer",
            "reason": "Too large — ordered wrong size",
            "status": "awaiting_drop_off",
            "return_label_url": f"https://returns.{brand_name.lower().replace(' ', '')}.com/RET-{prefix}-39854",
            "refund_amount": 58.00,
            "currency": currency,
        }],
    )

    # 4. Loyalty — delivered, recently
    c4 = f"{brand_id}_cust_004"
    customers[c4] = Customer(
        id=c4, brand_id=brand_id, email="chloe.thomas@example.com",
        tier=CustomerTier.LOYALTY, locale="en-GB", total_orders=7,
        recent_orders=[
            _order(c4, "50932", OrderStatus.DELIVERED, 5, 145.00,
                   _items(("COAT-WLK-BEG-14","Wool Blend Walker Coat",1,145.00,"14","Beige"))),
        ],
    )

    # 5. VIP — multiple orders, one faulty item return
    c5 = f"{brand_id}_cust_005"
    customers[c5] = Customer(
        id=c5, brand_id=brand_id, email="victoria.reed@example.com",
        tier=CustomerTier.VIP, locale="en-GB", total_orders=24,
        recent_orders=[
            _order(c5, "53109", OrderStatus.IN_TRANSIT, 1, 220.00, days_ahead=1,
                   items=_items(("DRSS-SLK-MID-12","Satin Midi Slip Dress",1,89.00,"12","Champagne"),
                                ("HLS-STL-BLK-8","Stiletto Heels",1,65.00,"8","Black"),
                                ("BAG-CLS-BRN","Structured Tote",1,66.00,None,"Brown"))),
            _order(c5, "49801", OrderStatus.RETURN_PENDING, 6, 54.00,
                   _items(("TOP-KNIT-FLW-10","Floral Print Knit Top",1,54.00,"10","Multi"))),
        ],
        open_returns=[{
            "return_id": f"RET-{prefix}-49801",
            "order_id": f"{prefix}-49801",
            "item": "Floral Print Knit Top",
            "reason": "Faulty — seam split after first wash",
            "status": "received_at_warehouse",
            "refund_amount": 54.00,
            "currency": currency,
            "faulty": True,
            "refund_eta_days": 3,
        }],
    )

    # 6. Standard — cancelled order
    c6 = f"{brand_id}_cust_006"
    customers[c6] = Customer(
        id=c6, brand_id=brand_id, email="mia.baker@example.com",
        tier=CustomerTier.STANDARD, locale="en-GB", total_orders=2,
        recent_orders=[
            _order(c6, "47633", OrderStatus.CANCELLED, 2, 29.99,
                   _items(("LGNG-RBD-BLK-S","Ribbed Leggings",1,29.99,"S","Black"))),
        ],
    )

    # ── Promo codes ────────────────────────────────────────────────────────
    promos = [
        {"code": f"{prefix}20NOW", "discount": "20% off full price items", "min_spend": 40.0,
         "expires": (now + timedelta(days=14)).isoformat(), "stackable": False},
        {"code": f"LOYALTY{prefix}", "discount": "Free standard delivery", "min_spend": 0.0,
         "expires": (now + timedelta(days=90)).isoformat(), "stackable": True,
         "tier_required": "loyalty"},
        {"code": "WELCOME10", "discount": "10% off first order", "min_spend": 25.0,
         "expires": (now + timedelta(days=365)).isoformat(), "stackable": False},
    ]

    # ── Mock integration tools ─────────────────────────────────────────────
    tools = {
        "order_tracking": {
            "description": "Look up live order status by order ID",
            "endpoint": f"https://api.{brand_name.lower().replace(' ', '')}.com/orders/{{order_id}}",
            "auth": "X-API-KEY",
        },
        "returns_portal": {
            "description": "Initiate or check a return request",
            "endpoint": f"https://returns.{brand_name.lower().replace(' ', '')}.com/api/{{order_id}}",
            "auth": "X-API-KEY",
        },
        "size_guide": {
            "description": "Size conversion tables for clothing categories",
            "endpoint": f"https://www.{brand_name.lower().replace(' ', '')}.com/size-guide",
            "auth": None,
        },
        "promo_validator": {
            "description": "Validate and apply a promotional code",
            "endpoint": f"https://api.{brand_name.lower().replace(' ', '')}.com/promos/validate",
            "auth": "X-API-KEY",
        },
    }

    return {
        "brand_id": brand_id,
        "brand_name": brand_name,
        "customers": customers,
        "promos": promos,
        "tools": tools,
        "generated_at": now.isoformat(),
    }


def get_mock_customer(customer_id: str | None, brand_id: str) -> Customer | None:
    if not customer_id:
        return None
    # Check dynamic demo store first (populated by POST /admin/brands/{id}/demo-data)
    if brand_id in _DEMO_DATA:
        return _DEMO_DATA[brand_id]["customers"].get(customer_id)
    return _MOCK_CUSTOMERS.get(customer_id)


# ═══════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/session/new", response_model=NewSessionResponse, tags=["Chat"])
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


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
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
    stored = session_data.get("messages", [])
    history_messages = []
    for m in stored:
        if isinstance(m, dict):
            if m.get("role") == "user":
                history_messages.append(HumanMessage(content=m["content"]))
            elif m.get("role") == "assistant":
                history_messages.append(AIMessage(content=m["content"]))
        else:
            history_messages.append(m)
    history_messages.append(HumanMessage(content=req.message))

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
        *(m.content if isinstance(m, (HumanMessage, AIMessage)) else m
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


@app.get("/session/{session_id}", tags=["Chat"])
async def get_session_info(session_id: str) -> dict[str, Any]:
    data = await get_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found.")
    # Strip messages for brevity — return metadata only
    return {k: v for k, v in data.items() if k != "messages"}


@app.post("/webhook/chatwoot", tags=["Chat"])
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

    # Resolve brand from inbox_id via registry
    from brands.registry import get_brand_by_chatwoot_inbox
    brand_entry = get_brand_by_chatwoot_inbox(inbox_id) if inbox_id else None
    brand_id = brand_entry.brand_id if brand_entry else "boohoo_uk"

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
        brand_id=brand_id,
        intent=response.intent,
    )

    return {"status": "processed", "session_id": session_id, "response": response.response}


@app.get("/health", tags=["System"], include_in_schema=True)
async def health():
    """
    Liveness probe — confirms the process is alive and the web server is responding.
    Does NOT check external dependencies.
    """
    return {"status": "ok", "service": "l1_support", "version": "0.1.0"}


@app.get("/ready", tags=["System"])
async def ready():
    """
    Readiness probe — confirms all external dependencies are reachable.
    Returns 200 if the service can accept traffic, 503 if any critical dependency is down.
    """
    checks: dict[str, str] = {}
    all_ok = True

    # FalkorDB
    try:
        _, graph = get_graph_client()
        graph.query("RETURN 1")
        checks["falkordb"] = "ok"
    except Exception as e:
        checks["falkordb"] = f"error: {e}"
        all_ok = False

    # Redis
    try:
        from core.memory.session import get_redis
        r = get_redis()
        await r.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"
        all_ok = False

    # Postgres (via psycopg)
    try:
        import psycopg
        conn_str = (
            f"host={settings.postgres_host} port={settings.postgres_port} "
            f"dbname={settings.postgres_db} user={settings.postgres_user} "
            f"password={settings.postgres_password}"
        )
        async with await psycopg.AsyncConnection.connect(conn_str) as conn:
            await conn.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"
        all_ok = False

    status_code = 200 if all_ok else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"status": "ready" if all_ok else "not_ready", "checks": checks},
        status_code=status_code,
    )


@app.get("/metrics", tags=["System"])
async def metrics():
    return PlainTextResponse(generate_latest(), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH / ONTOLOGY / KB / SYSTEM ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.middleware.auth import verify_admin_key

admin_router = APIRouter(prefix="/admin", dependencies=[Depends(verify_admin_key)])

# ── Graph endpoints ───────────────────────────────────────────────────────────
@admin_router.get("/graph/summary", tags=["Graph"])
async def graph_summary():
    """FalkorDB graph overview — node counts by label."""
    try:
        _, graph = get_graph_client()
        result = graph.query("MATCH (n) RETURN labels(n)[0] as label, count(n) as count ORDER BY count DESC")
        nodes = [{"label": r[0], "count": r[1]} for r in result.result_set if r[0]]
        return {"graph": settings.falkordb_host, "total_nodes": sum(n["count"] for n in nodes), "nodes": nodes}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@admin_router.get("/graph/relationships", tags=["Graph"])
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


@admin_router.get("/graph/brand/{brand_id}", tags=["Graph"])
async def graph_brand(brand_id: Annotated[str, Path(max_length=64)]):
    """All nodes connected to a specific brand. Use `/admin/brands` to get valid `brand_id` values."""
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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


@admin_router.get("/graph/issues", tags=["Graph"])
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


from core.schemas.validation import GraphQueryRequest, validate_brand_id

@admin_router.get("/graph/query", tags=["Graph"])
async def graph_query(q: Annotated[str, Field(min_length=1, max_length=2000)]):
    """Raw Cypher query against the knowledge graph. Example: `MATCH (n:Brand) RETURN n LIMIT 5`"""
    try:
        _, graph = get_graph_client()
        result = graph.query(q)
        return {"rows": [list(r) for r in result.result_set], "columns": result.header}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@admin_router.get("/ontology", tags=["Graph"])
async def ontology_overview():
    """Full ontology schema — node labels, relationship types, property keys."""
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


@admin_router.get("/ontology/types", tags=["Ontology"])
async def ontology_list_types():
    """All active entity types (built-in + dynamically proposed and applied)."""
    from core.ontology.dynamic import get_ontology_manager
    om = get_ontology_manager()
    return {"types": om.get_entity_types(include_builtin=True)}


@admin_router.get("/ontology/proposals", tags=["Ontology"])
async def ontology_list_proposals(status: str | None = None):
    """
    Ontology proposals raised by agents during collection.

    - `status=pending` — awaiting human approval
    - `status=active`  — already applied (auto or manual)
    - omit `status`    — return all
    """
    from core.ontology.dynamic import get_ontology_manager
    om = get_ontology_manager()
    return {"proposals": om.get_proposals(status=status)}


@admin_router.post("/ontology/proposals/{proposal_id}/apply", tags=["Ontology"])
async def ontology_apply_proposal(proposal_id: str):
    """Manually approve and activate a pending ontology proposal."""
    from core.ontology.dynamic import get_ontology_manager
    om = get_ontology_manager()
    ok = om.apply_proposal(proposal_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Proposal '{proposal_id}' not found or already active")
    return {"applied": True, "proposal_id": proposal_id}


@admin_router.post("/ontology/migrate-nodes", tags=["Ontology"])
async def migrate_nodes_to_ontology(dry_run: bool = True):
    """
    Retroactively re-label :KnowledgeItem nodes to match their `entity_type` property.

    **Problem**: Before this fix, all custom-typed items (SizeGuide, ContactMethod, etc.)
    were stored as `:KnowledgeItem` nodes — only the `entity_type` property told them apart.
    This means Cypher queries like `MATCH (n:SizeGuide)` return nothing.

    **Fix**: For each `:KnowledgeItem` node with a non-generic `entity_type`, this
    migration creates a properly-labelled replacement node, transfers all properties
    and the `HAS_KNOWLEDGE` relationship, then deletes the old node.

    Set `dry_run=false` to actually apply the migration. Defaults to dry-run.

    Returns a summary of what would be / was migrated per entity type.
    """
    try:
        _, graph = get_graph_client()

        # Discover all non-standard entity_type values used in KnowledgeItem nodes
        result = graph.query(
            "MATCH (k:KnowledgeItem) "
            "WHERE k.entity_type IS NOT NULL AND k.entity_type <> 'KnowledgeItem' "
            "RETURN k.entity_type as t, count(k) as c ORDER BY c DESC"
        )
        type_counts = {r[0]: r[1] for r in result.result_set if r[0]}

        if dry_run:
            return {
                "dry_run": True,
                "would_migrate": type_counts,
                "total_nodes": sum(type_counts.values()),
                "message": "Re-run with ?dry_run=false to apply migration",
            }

        migrated: dict[str, int] = {}
        for entity_type in type_counts:
            # Validate label
            import re as _re
            if not _re.match(r'^[A-Za-z][A-Za-z0-9_]*$', entity_type):
                migrated[entity_type] = 0
                continue

            # Fetch all KnowledgeItem nodes with this entity_type
            nodes = graph.query(
                f"MATCH (k:KnowledgeItem) WHERE k.entity_type = '{entity_type}' "
                f"RETURN k.id, k.title, k.content, k.category, k.brand_id, k.source_url, k.extracted_at"
            )

            count = 0
            for row in nodes.result_set:
                nid, title, content, cat, brand_id, src, ext = row
                if not nid:
                    continue
                t = (title or "").replace("'", "")
                c = (content or "").replace("'", "")
                ca = (cat or "general").replace("'", "")
                s = (src or "").replace("'", "")
                e = (ext or "").replace("'", "")
                bid = (brand_id or "").replace("'", "")

                # Create new node with proper label
                graph.query(
                    f"MERGE (n:{entity_type} {{id:'{nid}'}}) "
                    f"SET n.entity_type='{entity_type}',n.title='{t}',"
                    f"    n.content='{c}',n.category='{ca}',"
                    f"    n.brand_id='{bid}',n.source_url='{s}',n.extracted_at='{e}' "
                    f"WITH n MATCH (b:Brand {{id:'{bid}'}}) MERGE (b)-[:HAS_KNOWLEDGE]->(n)"
                )
                # Delete old KnowledgeItem node
                graph.query(
                    f"MATCH (k:KnowledgeItem {{id:'{nid}'}}) DETACH DELETE k"
                )
                count += 1

            migrated[entity_type] = count
            log.info("ontology_migration_type", entity_type=entity_type, migrated=count)

        return {
            "dry_run": False,
            "migrated": migrated,
            "total_migrated": sum(migrated.values()),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.get("/ontology/brand/{brand_id}", tags=["Graph"])
async def ontology_brand(brand_id: Annotated[str, Path(max_length=64)]):
    """
    Brand knowledge subtree — policies, issue types, FAQs, and knowledge items.

    Note: Brand nodes are keyed by `id` property (not `brand_id`).
    Also queries directly by brand_id property on child nodes as fallback.
    """
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        _, graph = get_graph_client()

        # Policies — via Brand node (id=brand_id) or direct brand_id property
        policies = graph.query(
            f"MATCH (p:Policy) WHERE p.brand_id = '{brand_id}' "
            f"RETURN p.policy_type as type, p.title as title LIMIT 20"
        )
        issues = graph.query(
            f"MATCH (b:Brand {{id: '{brand_id}'}})-[:HAS_ISSUE_TYPE]->(it) "
            f"RETURN it.issue_type_id as issue, it.category as category"
        )
        faqs = graph.query(
            f"MATCH (f:FAQ) WHERE f.brand_id = '{brand_id}' "
            f"RETURN f.question as question, f.category as category LIMIT 10"
        )
        knowledge = graph.query(
            f"MATCH (k:KnowledgeItem) WHERE k.brand_id = '{brand_id}' "
            f"RETURN k.title as title, k.entity_type as type, k.category as category LIMIT 10"
        )
        return {
            "brand_id": brand_id,
            "policies": [dict(zip(["type", "title"], r)) for r in policies.result_set],
            "issue_types": [dict(zip(["issue", "category"], r)) for r in issues.result_set],
            "sample_faqs": [dict(zip(["question", "category"], r)) for r in faqs.result_set],
            "knowledge_items": [dict(zip(["title", "type", "category"], r)) for r in knowledge.result_set],
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Knowledge Base endpoints ──────────────────────────────────────────────────
@admin_router.get("/kb/faqs", tags=["Knowledge Base"])
async def kb_faqs(brand_id: Annotated[str | None, Query(max_length=64)] = None, category: str | None = None):
    """FAQs from FalkorDB — filter by `brand_id` and/or `category`."""
    if brand_id:
        try:
            brand_id = validate_brand_id(brand_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
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


@admin_router.get("/kb/policies", tags=["Knowledge Base"])
async def kb_policies(brand_id: Annotated[str | None, Query(max_length=64)] = None):
    """Policies from FalkorDB — optionally filtered by `brand_id`."""
    if brand_id:
        try:
            brand_id = validate_brand_id(brand_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
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


@admin_router.get("/kb/pgvector/chunks", tags=["Knowledge Base"])
async def kb_chunks(brand_id: Annotated[str | None, Query(max_length=64)] = None, chunk_type: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 20):
    """Semantic-search chunks from Postgres pgvector — the base for vector retrieval."""
    if brand_id:
        try:
            brand_id = validate_brand_id(brand_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if not settings.postgres_host:
        return {"count": 0, "chunks": [], "note": "Postgres not configured"}
    try:
        import psycopg
        conn_str = (
            f"host={settings.postgres_host} port={settings.postgres_port} "
            f"dbname={settings.postgres_db} user={settings.postgres_user} "
            f"password={settings.postgres_password}"
        )
        async with await psycopg.AsyncConnection.connect(conn_str) as conn:
            params: list = []
            q = "SELECT id, brand_id, chunk_type, source_url, left(content,200), metadata FROM knowledge_chunks WHERE 1=1"
            if brand_id:
                q += " AND brand_id = %s"
                params.append(brand_id)
            if chunk_type:
                q += " AND chunk_type = %s"
                params.append(chunk_type)
            q += " ORDER BY crawled_at DESC LIMIT %s"
            params.append(limit)
            rows = await (await conn.execute(q, params)).fetchall()
            return {
                "count": len(rows),
                "chunks": [
                    {"id": str(r[0]), "brand_id": r[1], "chunk_type": r[2],
                     "source_url": r[3], "preview": r[4], "metadata": r[5]}
                    for r in rows
                ],
            }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Brand endpoints ───────────────────────────────────────────────────────────

class BrandRegisterRequest(BaseModel):
    brand_id: str
    name: str
    domains: list[str]
    locales: list[str] = ["en-GB"]
    default_region: str = "UK"
    chatwoot_inbox_id: int | None = None


@admin_router.get("/brands", tags=["Brands"])
async def list_brands():
    """All registered brands — `brand_id` is the key used in every other endpoint."""
    from brands.registry import BRAND_REGISTRY
    return {
        "count": len(BRAND_REGISTRY),
        "brands": [
            {
                "brand_id": b.brand_id,
                "name": b.name,
                "domains": b.domains,
                "locales": b.locales,
                "default_region": b.default_region,
                "chatwoot_inbox_id": b.chatwoot_inbox_id,
            }
            for b in BRAND_REGISTRY
        ],
    }


@admin_router.post("/brands", tags=["Brands"], status_code=201)
async def add_brand(req: BrandRegisterRequest):
    """
    Register a new brand.

    After registration the brand_id is immediately usable in `/chat`, `/session/new`,
    and all other endpoints. The entry is persisted to Redis so it survives restarts.

    To also seed the knowledge graph with ontology data, call
    `POST /admin/ingest` with the new `brand` value.
    """
    from brands.registry import BrandEntry, register_brand
    entry = BrandEntry(
        brand_id=req.brand_id,
        name=req.name,
        domains=req.domains,
        locales=req.locales,
        default_region=req.default_region,
        chatwoot_inbox_id=req.chatwoot_inbox_id,
    )
    added = register_brand(entry)
    if not added:
        raise HTTPException(status_code=409, detail=f"Brand '{req.brand_id}' already exists.")

    # Persist to Redis so dynamic brands survive restarts
    try:
        import json
        from core.memory.session import get_redis
        r = get_redis()
        await r.hset("brand_registry", req.brand_id, json.dumps({
            "brand_id": req.brand_id,
            "name": req.name,
            "domains": req.domains,
            "locales": req.locales,
            "default_region": req.default_region,
            "chatwoot_inbox_id": req.chatwoot_inbox_id,
        }))
    except Exception as e:
        log.warning("brand_redis_persist_failed", brand_id=req.brand_id, error=str(e))

    # Also create the Brand node in FalkorDB so the graph reflects the new brand
    try:
        from core.ontology.models import Brand, BrandTier
        from core.ontology.seed_data import upsert_brand, get_graph_client
        graph_ok, graph = get_graph_client()
        if graph_ok:
            brand_for_graph = Brand(
                id=entry.brand_id,
                name=entry.name,
                group_id="boohoo_group",
                domain=entry.domains[0] if entry.domains else f"{entry.brand_id}.com",
                help_center_url=f"https://{entry.domains[0]}/help" if entry.domains else "https://example.com/help",
                default_locale=entry.locales[0] if entry.locales else "en-GB",
                tier=BrandTier.MID,
                target_demographic="Customers",
                voice_persona="Professional and helpful",
                chatwoot_inbox_id=entry.chatwoot_inbox_id,
            )
            upsert_brand(graph, brand_for_graph)
            log.info("brand_graph_node_created", brand_id=entry.brand_id)
    except Exception as e:
        log.warning("brand_graph_node_failed", brand_id=entry.brand_id, error=str(e))

    log.info("brand_registered", brand_id=req.brand_id, name=req.name, domains=req.domains)
    return {
        "registered": True,
        "brand_id": req.brand_id,
        "name": req.name,
        "next_step": f"Call POST /admin/brands/{req.brand_id}/collect to crawl the web and populate the knowledge graph.",
    }


@admin_router.get("/brands/{brand_id}", tags=["Brands"])
async def get_brand_detail(brand_id: Annotated[str, Path(max_length=64)]):
    """Single brand detail. Call `GET /admin/brands` first to see valid `brand_id` values."""
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from brands.registry import get_brand_by_id
    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not found. GET /admin/brands lists all valid brand_ids.")
    return {
        "brand_id": entry.brand_id,
        "name": entry.name,
        "domains": entry.domains,
        "locales": entry.locales,
        "default_region": entry.default_region,
        "chatwoot_inbox_id": entry.chatwoot_inbox_id,
    }


@admin_router.post("/brands/{brand_id}/demo-data", tags=["Operations"])
async def seed_brand_demo_data(brand_id: Annotated[str, Path(max_length=64)]):
    """
    Seed realistic mock demo data for a brand — simulates the customer/order
    data that the brand's own backend systems would provide in production.

    Generates and stores in-memory:
    - **6 customers** spanning Standard / Loyalty / VIP tiers
    - **Multiple orders per customer** with statuses: PROCESSING, IN_TRANSIT,
      DELIVERED, RETURN_PENDING, CANCELLED
    - **Shipment tracking** info for dispatched/in-transit orders
    - **Open returns** (size issue, faulty item) with return labels
    - **3 promo codes** (percentage discount, free delivery, welcome offer)
    - **Mock integration tools** (order tracking API, returns portal, size guide,
      promo validator) — these are the endpoints the support agent would call
      at runtime

    After seeding, use the `customer_id` values returned here in `POST /chat`
    requests to get context-aware responses. E.g. a customer with a
    RETURN_PENDING order asking "where is my return?" will get a specific reply.

    Re-calling this endpoint regenerates the data (new tracking IDs, fresh timestamps).
    """
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from brands.registry import get_brand_by_id
    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not found.")

    data = _generate_brand_demo_data(brand_id, entry.name)
    _DEMO_DATA[brand_id] = data

    return {
        "seeded": True,
        "brand_id": brand_id,
        "brand_name": entry.name,
        "summary": {
            "customers": len(data["customers"]),
            "customer_ids": list(data["customers"].keys()),
            "promos": len(data["promos"]),
            "promo_codes": [p["code"] for p in data["promos"]],
            "tools": list(data["tools"].keys()),
        },
        "generated_at": data["generated_at"],
        "usage": (
            f"Pass any customer_id from `customer_ids` as the `customer_id` field in "
            f"POST /chat or POST /session/new to get context-aware support responses. "
            f"Example: POST /chat {{\"session_id\": \"...\", \"message\": \"where is my order?\", "
            f"\"customer_id\": \"{brand_id}_cust_001\"}}"
        ),
    }


# ── Session endpoints ─────────────────────────────────────────────────────────
@admin_router.get("/sessions", tags=["Sessions"])
async def list_sessions(brand_id: Annotated[str | None, Query(max_length=64)] = None, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    """All active sessions in Redis — optionally filtered by `brand_id`."""
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


@admin_router.get("/sessions/{session_id}", tags=["Sessions"])
async def get_session_detail(session_id: str):
    """Full session state including message history."""
    data = await get_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


# ── System endpoints ──────────────────────────────────────────────────────────
@admin_router.get("/overview", tags=["System"])
async def system_overview():
    """Full system snapshot — all services, graph stats, KB chunk counts, active sessions."""
    from core.memory.session import get_redis

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

    session_count = 0
    try:
        r = get_redis()
        async for _ in r.scan_iter(match="session:*"):
            session_count += 1
    except Exception:
        pass

    pg_stats = {}
    if settings.postgres_host:
        try:
            import psycopg
            conn_str = (
                f"host={settings.postgres_host} port={settings.postgres_port} "
                f"dbname={settings.postgres_db} user={settings.postgres_user} "
                f"password={settings.postgres_password}"
            )
            async with await psycopg.AsyncConnection.connect(conn_str) as conn:
                rows = await (await conn.execute(
                    "SELECT brand_id, count(*) FROM knowledge_chunks GROUP BY brand_id"
                )).fetchall()
                pg_stats = {r[0]: r[1] for r in rows}
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


# ── Brand data collection endpoints ──────────────────────────────────────────

class CollectRequest(BaseModel):
    base_url: str | None = None
    resume: bool = True
    max_urls: Annotated[int, Field(ge=1, le=500)] = 200
    concurrency: Annotated[int, Field(ge=1, le=20)] = 5


@admin_router.post("/brands/{brand_id}/collect", tags=["Brands"])
async def trigger_brand_collection(brand_id: Annotated[str, Path(max_length=64)], req: CollectRequest):
    """
    Trigger agent-driven data collection for a registered brand.

    **Pipeline (3 nodes):**
    1. **Orchestrate** — discovers real help-centre URLs via sitemap, DuckDuckGo,
       and stealth browser crawl of the brand's homepage.
    2. **Collect** — crawl4ai BFSDeepCrawlStrategy follows only links actually found
       in the DOM (no hallucinated paths). Falls back to manual BFS with stealth
       browser → Google Cache → Wayback Machine waterfall.
    3. **Synthesize** — writes FAQs/policies/knowledge items to FalkorDB and
       pgvector; applies any ontology proposals raised during extraction.

    Check progress with `GET /admin/brands/{brand_id}/collect/status`.
    """
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from brands.registry import get_brand_by_id
    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not found. Register it first via POST /admin/brands.")

    base_url = req.base_url or (f"https://{entry.domains[0]}" if entry.domains else f"https://{brand_id}.com")

    async def _run_in_background():
        from ingestion.agent_collector import run_agent_collection, _save_status
        try:
            result = await run_agent_collection(
                brand_id=brand_id,
                brand_name=entry.name,
                base_url=base_url,
                max_urls=req.max_urls,
            )
            await _save_status(brand_id, result)
        except Exception as e:
            log.error("agent_collection_failed", brand_id=brand_id, error=str(e))

    asyncio.create_task(_run_in_background())

    return {
        "status": "started",
        "brand_id": brand_id,
        "brand_name": entry.name,
        "base_url": base_url,
        "max_urls": req.max_urls,
        "message": f"Agent collection running in background. Poll GET /admin/brands/{brand_id}/collect/status for progress.",
    }


@admin_router.get("/brands/{brand_id}/collect/status", tags=["Brands"])
async def brand_collection_status(brand_id: Annotated[str, Path(max_length=64)]):
    """
    Check the current data collection status for a brand.

    Returns phase, extracted_count, pages_visited, ontology_proposals, errors.
    Returns 404 if no collection has been started for this brand.
    """
    from ingestion.agent_collector import get_collection_status
    status = await get_collection_status(brand_id)
    if not status:
        raise HTTPException(
            status_code=404,
            detail=f"No collection started for '{brand_id}'. Call POST /admin/brands/{brand_id}/collect first.",
        )
    return status


@admin_router.get("/brands/{brand_id}/probe", tags=["Brands"])
async def probe_brand_scraping(brand_id: Annotated[str, Path(max_length=64)]):
    """
    Probe a brand's website to determine whether bot protection is active
    and which scraping strategy is recommended.

    Returns:
    - **plain_httpx**: whether plain HTTP GET works at all
    - **content_rich**: whether the plain response has real page content (>2000 chars)
    - **bot_protected**: whether bot-gate signals (Cloudflare, CAPTCHA, etc.) were detected
    - **recommended_strategy**: `httpx` (no protection) | `stealth` (Playwright) | `firecrawl` (cloud bypass)
    - **firecrawl_available**: whether a FireCrawl API key is configured

    Brands that return **recommended_strategy: httpx** can be scraped cheaply without
    launching a headless browser — much faster and more reliable.
    """
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from brands.registry import get_brand_by_id
    from ingestion.agent_collector import probe_site_access

    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not found.")

    base_url = f"https://{entry.domains[0]}" if entry.domains else f"https://{brand_id}.com"
    report = await probe_site_access(base_url)
    return report


@admin_router.post("/brands/{brand_id}/documents/upload", tags=["Operations"])
async def upload_brand_documents(
    brand_id: str,
    files: list[UploadFile] = File(...),
    x_api_key: str | None = Header(None, alias="X-API-KEY"),
):
    """
    Upload brand documents (PDF, DOCX, TXT, MD, HTML) to be ingested into the
    knowledge graph and pgvector store.

    Uploaded files are parsed into text chunks, each chunk is sent through the
    same LLM extraction pipeline used for web-crawled pages, and the results
    are written to FalkorDB + pgvector under the given brand_id.

    **Supports:** `.pdf`, `.docx`, `.doc`, `.txt`, `.md`, `.html`, `.htm`

    **Auth:** `X-API-KEY` header required.

    Returns extraction counts and any per-file errors.
    """
    if x_api_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    from brands.registry import get_brand_by_id
    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not found.")

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    import tempfile
    from ingestion.agent_collector import collect_from_documents
    from ingestion.doc_parser import SUPPORTED_EXTENSIONS
    from pathlib import Path as _Path

    saved_paths: list[str] = []
    skipped: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for upload in files:
            fname = upload.filename or "upload"
            ext = _Path(fname).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                skipped.append({"file": fname, "reason": f"unsupported type: {ext}"})
                continue
            dest = _os.path.join(tmpdir, fname)
            content = await upload.read()
            with open(dest, "wb") as f:
                f.write(content)
            saved_paths.append(dest)

        if not saved_paths:
            return {
                "brand_id": brand_id,
                "extracted_count": 0,
                "files_processed": 0,
                "skipped": skipped,
                "errors": ["No valid files to process"],
            }

        result = await collect_from_documents(
            brand_id=brand_id,
            brand_name=entry.name,
            file_paths=saved_paths,
        )

    result["skipped"] = result.get("skipped", []) + skipped
    return result


# ═══════════════════════════════════════════════════════════════════════════
# API REGISTRY ADMIN ENDPOINTS — dynamic brand API tools
# ═══════════════════════════════════════════════════════════════════════════

class CurlParseRequest(BaseModel):
    """Parse a curl command and extract endpoint details."""
    curl_command: str
    name: str | None = None
    description: str | None = None
    tags: list[str] = []
    rate_limit_per_min: int | None = None


class EndpointRegisterRequest(BaseModel):
    """Register an endpoint explicitly (alternative to curl paste)."""
    name: str
    description: str = ""
    method: str = "GET"
    path: str
    query_params: list[dict] = []
    body_schema: dict | None = None
    response_schema: dict | None = None
    rate_limit_per_min: int | None = None
    requires_auth: bool = False
    tags: list[str] = []
    curl_command: str | None = None  # optional: derive method/path from curl


class CredentialStoreRequest(BaseModel):
    """Store encrypted credentials for a brand's API."""
    key_type: str  # api_key | bearer_token | basic_auth | oauth2
    key_name: str
    value: str


@admin_router.post("/api-config/register", tags=["API Registry"])
async def register_api_config(brand_id: Annotated[str, Query(max_length=64)], base_url: str, description: str = ""):
    """
    Register (or update) a brand's API configuration.
    Base URL is the root — endpoints are added separately.
    """
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from core.tools.api_registry import register_brand_api
    try:
        config_id = register_brand_api(brand_id=brand_id, base_url=base_url, description=description)
        return {"success": True, "config_id": config_id, "brand_id": brand_id}
    except Exception as e:
        log.error("api_config_register_failed", brand_id=brand_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.post("/api-config/curl-parse", tags=["API Registry"])
async def parse_curl_command(req: CurlParseRequest):
    """
    Parse a curl command and extract the method, URL, headers, and body.
    This is a preview endpoint — use /api-config/endpoint to actually register it.
    """
    from core.tools.api_registry import parse_curl
    try:
        parsed = parse_curl(req.curl_command)
        return {
            "method": parsed.method,
            "url": parsed.url,
            "headers": parsed.headers,
            "body": parsed.body,
            "auth": parsed.auth,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@admin_router.post("/api-config/endpoints", tags=["API Registry"], status_code=201)
async def register_endpoint(req: EndpointRegisterRequest):
    """
    Register a single API endpoint for a brand.

    Either provide explicit params (method, path, etc.) or pass a `curl_command`
    to auto-extract method, path, and body schema.
    """
    from core.tools.api_registry import register_brand_api, register_endpoint as reg_ep

    # Resolve brand_api_config_id
    from core.tools.api_registry import get_brand_config
    config = get_brand_config(req.name.rsplit("_", 1)[0] if "_" in req.name else req.name)
    if not config:
        raise HTTPException(status_code=404, detail=f"No API config found for brand. Call POST /admin/api-config/register first.")

    try:
        endpoint_id = reg_ep(
            brand_api_config_id=config["id"],
            name=req.name,
            description=req.description,
            method=req.method,
            path=req.path,
            query_params=req.query_params or [],
            body_schema=req.body_schema,
            response_schema=req.response_schema,
            rate_limit_per_min=req.rate_limit_per_min,
            requires_auth=req.requires_auth,
            tags=req.tags,
            curl_command=req.curl_command,
        )
        return {"success": True, "endpoint_id": endpoint_id, "name": req.name}
    except Exception as e:
        log.error("endpoint_register_failed", name=req.name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.post("/api-config/{brand_id}/credentials", tags=["API Registry"], status_code=201)
async def store_credential(brand_id: str, req: CredentialStoreRequest):
    """Store an encrypted credential (API key, bearer token, etc.) for a brand."""
    from core.tools.api_registry import get_brand_config, store_credentials
    from brands.registry import get_brand_by_id

    # Validate brand exists
    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not registered. Call POST /admin/brands first.")

    config = get_brand_config(brand_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"No API config for '{brand_id}'. Call POST /admin/api-config/register first.")

    try:
        cred_id = store_credentials(
            brand_api_config_id=config["id"],
            key_type=req.key_type,
            key_name=req.key_name,
            value=req.value,
        )
        return {"success": True, "credential_id": cred_id, "key_name": req.key_name}
    except Exception as e:
        log.error("credential_store_failed", brand_id=brand_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.get("/api-config/{brand_id}/endpoints", tags=["API Registry"])
async def list_brand_endpoints(brand_id: Annotated[str, Path(max_length=64)]):
    """List all registered API endpoints for a brand."""
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from core.tools.api_registry import list_endpoints_for_brand
    from brands.registry import get_brand_by_id

    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not found")

    try:
        endpoints = list_endpoints_for_brand(brand_id)
        return {
            "brand_id": brand_id,
            "count": len(endpoints),
            "endpoints": [
                {
                    "id": e.id,
                    "name": e.name,
                    "description": e.description,
                    "method": e.method,
                    "path": e.path,
                    "tags": e.tags,
                    "requires_auth": e.requires_auth,
                    "rate_limit_per_min": e.rate_limit_per_min,
                }
                for e in endpoints
            ],
        }
    except Exception as e:
        log.error("list_endpoints_failed", brand_id=brand_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.get("/api-config/{brand_id}/tools", tags=["API Registry"])
async def get_brand_tools(brand_id: Annotated[str, Path(max_length=64)]):
    """Get the LLM tool manifest for a brand — all available dynamic tools."""
    try:
        brand_id = validate_brand_id(brand_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from core.tools.dynamic_builder import get_tool_manifest_summary
    from brands.registry import get_brand_by_id

    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not found")

    try:
        tools = get_tool_manifest_summary(brand_id)
        return {"brand_id": brand_id, "count": len(tools), "tools": tools}
    except Exception as e:
        log.error("tool_manifest_failed", brand_id=brand_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.post("/api-config/test/{endpoint_id}", tags=["API Registry"])
async def test_endpoint(endpoint_id: str, test_params: dict | None = None):
    """Test a registered endpoint by firing a real HTTP request."""
    from core.tools.api_registry import test_endpoint as test_ep
    try:
        result = test_ep(endpoint_id, test_params)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.delete("/api-config/endpoints/{endpoint_id}", tags=["API Registry"])
async def delete_endpoint(endpoint_id: str):
    """Soft-delete an endpoint (marks inactive)."""
    from core.tools.api_registry import delete_endpoint as del_ep
    try:
        deleted = del_ep(endpoint_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        return {"success": True, "endpoint_id": endpoint_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@admin_router.get("/api-config/demo/endpoints", tags=["API Registry"])
async def list_demo_api_endpoints():
    """
    List all endpoints available in the built-in demo ecommerce API.
    Use these to quickly wire the demo API into a brand without needing to curl-parse.
    """
    return {
        "description": "Demo Ecommerce API — seeded with 13 brands, 65 customers, 186 orders",
        "base_url": "http://192.168.0.105:8001",
        "endpoints": [
            {"name": "get_customer", "method": "GET", "path": "/api/v1/customers/{customer_id}", "tags": ["customer"], "requires_auth": False, "description": "Get customer details with optional order history"},
            {"name": "search_customers", "method": "GET", "path": "/api/v1/customers", "tags": ["customer"], "requires_auth": False, "description": "Search customers by email or name"},
            {"name": "get_order", "method": "GET", "path": "/api/v1/orders/{order_id}", "tags": ["orders"], "requires_auth": False, "description": "Get order details with items and shipment"},
            {"name": "list_orders", "method": "GET", "path": "/api/v1/orders", "tags": ["orders"], "requires_auth": False, "description": "List orders filtered by customer_id or status"},
            {"name": "get_order_status", "method": "GET", "path": "/api/v1/orders/{order_id}/status", "tags": ["orders", "wismo"], "requires_auth": False, "description": "Quick order status check for WISMO queries"},
            {"name": "get_return", "method": "GET", "path": "/api/v1/returns/{return_id}", "tags": ["returns"], "requires_auth": False, "description": "Get return details with items"},
            {"name": "get_refund_status", "method": "GET", "path": "/api/v1/returns/{return_id}/refund-status", "tags": ["returns", "refund"], "requires_auth": False, "description": "Quick refund status — days elapsed, overdue flag, expected date"},
            {"name": "initiate_return", "method": "POST", "path": "/api/v1/returns", "tags": ["returns"], "requires_auth": False, "description": "Initiate a return for an order"},
            {"name": "track_shipment", "method": "GET", "path": "/api/v1/shipments/track", "tags": ["wismo", "shipping"], "requires_auth": False, "description": "Track shipment by order_id or tracking_id"},
            {"name": "get_promo", "method": "POST", "path": "/api/v1/promos/validate", "tags": ["promos"], "requires_auth": False, "description": "Validate promo code — body: {code, basket_value}"},
            {"name": "account_status", "method": "GET", "path": "/api/v1/accounts/status", "tags": ["account"], "requires_auth": False, "description": "Check account status by customer_id or email"},
        ],
    }


@admin_router.post("/api-config/demo/wire-brand", tags=["API Registry"])
async def wire_demo_api_for_brand(brand_id: Annotated[str, Path(max_length=64)]):
    """
    One-shot wire: register the demo API for a brand and all its endpoints.
    Use this for testing — production brands should register via curl paste or manual config.
    """
    from core.tools.api_registry import (
        get_brand_config, register_brand_api,
        register_endpoint as reg_ep, list_endpoints_for_brand,
    )
    from brands.registry import get_brand_by_id

    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Brand '{brand_id}' not registered. Call POST /admin/brands first.")

    # Register base config
    config_id = register_brand_api(
        brand_id=brand_id,
        base_url="http://192.168.0.105:8001",
        description="Demo Ecommerce API — SQLite-backed mock of real ecommerce platform",
    )

    # List existing endpoints to check what needs adding
    existing = {e.name for e in list_endpoints_for_brand(brand_id)}

    endpoints_to_add = [
        ("get_customer", "GET", "/api/v1/customers/{customer_id}", "customer", "Get customer details by customer_id. Query: include_orders=true for order history."),
        ("search_customers", "GET", "/api/v1/customers", "customer", "Search customers by email or name query params."),
        ("get_order", "GET", "/api/v1/orders/{order_id}", "orders", "Get order details including items and shipment info."),
        ("list_orders", "GET", "/api/v1/orders", "orders", "List orders. Filter by customer_id or status query params."),
        ("get_order_status", "GET", "/api/v1/orders/{order_id}/status", "wismo", "Quick status check for WISMO."),
        ("get_return", "GET", "/api/v1/returns/{return_id}", "returns", "Get return details."),
        ("get_refund_status", "GET", "/api/v1/returns/{return_id}/refund-status", "refund", "Get refund status."),
        ("initiate_return", "POST", "/api/v1/returns", "returns", "Initiate a return."),
        ("track_shipment", "GET", "/api/v1/shipments/track", "wismo", "Track shipment by order_id or tracking_id."),
        ("account_status", "GET", "/api/v1/accounts/status", "account", "Check customer account status."),
    ]

    registered = []
    errors = []
    for name, method, path, tag, description in endpoints_to_add:
        if name in existing:
            continue
        try:
            ep_id = reg_ep(
                brand_api_config_id=config_id,
                name=name,
                description=description,
                method=method,
                path=path,
                requires_auth=False,
                tags=[tag],
            )
            registered.append({"name": name, "id": ep_id})
        except Exception as e:
            errors.append({"name": name, "error": str(e)})

    return {
        "success": True,
        "brand_id": brand_id,
        "config_id": config_id,
        "endpoints_registered": len(registered),
        "endpoints_skipped": len(existing),
        "errors": errors,
        "registered": registered,
    }


# Register admin router
app.include_router(admin_router)


# ═══════════════════════════════════════════════════════════════════════════
# VOICE ENDPOINTS (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════

class VoiceStartRequest(BaseModel):
    brand: str
    customer_name: str | None = None


@app.post("/voice/incoming", tags=["Voice"])
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


@app.post("/voice/start", tags=["Voice"])
async def voice_start(req: VoiceStartRequest):
    """Dev endpoint — start a voice call session and return the Daily room URL to join."""
    brand_entry = resolve_brand(req.brand)
    if not brand_entry:
        raise HTTPException(status_code=400, detail=f"Unknown brand: {req.brand}")

    from channels.voice.router import start_dev_call
    call_info = await start_dev_call(brand_id=brand_entry.brand_id)
    return call_info


@app.get("/voice/active", tags=["Voice"])
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


@app.post("/admin/ingest", tags=["System"])
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


@app.get("/admin/jobs", tags=["System"])
async def admin_jobs():
    """List all scheduled ingestion jobs."""
    from ingestion.scheduler import list_jobs
    return {"jobs": list_jobs()}


# ── Import SessionOutcome for the chat endpoint ────────────────────────────
from core.ontology.models import SessionOutcome  # noqa: E402 — needed after app definition


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATIONS — external MCP servers & REST APIs provided by companies
# ═══════════════════════════════════════════════════════════════════════════
# Companies provide their own MCP server URLs + auth headers.
# Register them here; the agent will call those tools at runtime.

import json as _json

_integrations_router = APIRouter(prefix="/integrations", tags=["Integrations"])

# In-memory store for the demo; swap for Redis/DB in production
_registered_integrations: dict[str, dict] = {}


class MCPServerRegistration(BaseModel):
    name: str
    url: str
    description: str = ""
    auth_headers: dict[str, str] = {}
    type: str = "mcp"          # "mcp" | "rest"
    extra: dict[str, Any] = {}  # e.g. {"openapi_spec_url": "..."}


@_integrations_router.post("/mcp", summary="Register an external MCP server")
async def register_mcp_server(reg: MCPServerRegistration):
    """
    Register a company-provided MCP server.

    - **name**: unique identifier (e.g. `acme_crm`)
    - **url**: SSE endpoint URL the company gave you (e.g. `https://api.acme.io/mcp/sse`)
    - **auth_headers**: any auth headers required (e.g. `{"Authorization": "Bearer <token>"}`)
    - **type**: `mcp` for Model Context Protocol servers, `rest` for plain REST APIs
    """
    _registered_integrations[reg.name] = reg.model_dump()
    log.info("integration_registered", name=reg.name, url=reg.url, type=reg.type)
    return {"registered": reg.name, "total": len(_registered_integrations)}


@_integrations_router.get("/mcp", summary="List registered external integrations")
async def list_mcp_servers():
    """List all registered external MCP / REST integrations."""
    return {
        "count": len(_registered_integrations),
        "integrations": [
            {
                "name": v["name"],
                "url": v["url"],
                "type": v["type"],
                "description": v["description"],
                # never echo secrets back — just confirm headers are set
                "auth_headers_configured": list(v["auth_headers"].keys()),
            }
            for v in _registered_integrations.values()
        ],
    }


@_integrations_router.delete("/mcp/{name}", summary="Remove a registered integration")
async def delete_mcp_server(name: str):
    """Remove an external integration by name."""
    if name not in _registered_integrations:
        raise HTTPException(status_code=404, detail=f"Integration '{name}' not found")
    del _registered_integrations[name]
    return {"removed": name}


@_integrations_router.get("/mcp/{name}/probe", summary="Probe an external MCP server for its tool list")
async def probe_mcp_server(name: str):
    """
    Attempt to fetch the tool list from a registered MCP server.
    Calls the server's `/tools/list` endpoint (or equivalent) using the stored auth headers.
    """
    reg = _registered_integrations.get(name)
    if not reg:
        raise HTTPException(status_code=404, detail=f"Integration '{name}' not found. Register it first via POST /integrations/mcp.")

    import httpx
    headers = reg.get("auth_headers", {})
    base_url = reg["url"].rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                base_url,
                headers={"Content-Type": "application/json", **headers},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            resp.raise_for_status()
            data = resp.json()
            tools = data.get("result", {}).get("tools", [])
            return {"integration": name, "tool_count": len(tools), "tools": tools}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach '{name}' at {base_url}: {e}")


app.include_router(_integrations_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, log_level=settings.log_level.lower())
