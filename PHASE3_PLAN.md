# Phase 3 — Production Readiness & Live Knowledge Base

## Context

Phase 1 built the core orchestration (LangGraph, FalkorDB schema, mock tools, context engine).
Phase 2 delivered real memory (Graphiti + ChromaDB), the crawl-to-graph ingestion pipeline,
8 specialist Swarms agents, and the Pipecat voice channel. 114 tests pass against live services.

Phase 3 makes the system production-ready: real integrations, live knowledge, feedback loops,
observability, and multi-brand onboarding.

---

## Phase 3A — Live Knowledge Base (current sprint)

### Goal
Replace all seed stubs with real, accurate brand content that agents can actually use.

### Deliverables

**3A-1: Boohoo KB content layer** (`data/boohoo_kb/`)
- `policies.py` — Full return, shipping, payment, cancellation policies (UK/EU/US variants)
- `faqs.py` — 60+ Q&A pairs covering all 11 issue categories with realistic wording
- `brand_voice.py` — Tone guide, forbidden phrases, escalation scripts per brand tier
- `seed_boohoo.py` — Idempotent loader: FalkorDB FAQ nodes + pgvector embeddings

**3A-2: Context engine end-to-end validation**
- `tests/test_e2e_boohoo.py` — Simulates real agent interactions against seeded KB:
  WISMO → context assembled with tracking steps and policy block
  Returns → context includes 28-day window, £1.99 fee, excluded categories
  Refund delay → context includes timeline (warehouse receipt → bank processing)
  Promo → context includes code conditions and exclusions
  Account → context includes security rules and unlock procedure

**3A-3: Ingestion pipeline smoke test**
- Run full crawl-to-graph on live Boohoo help center pages
- Validate confidence scores on extracted policies
- Confirm dedup prevents duplicate FAQ nodes

---

## Phase 3B — Real Tool Integrations

### Goal
Replace mock tools with a pluggable backend that routes to real APIs or configurable stubs
(for demo/staging environments without live OMS access).

### Deliverables

**3B-1: Tool backend abstraction** (`core/tools/backends/`)
- `interface.py` — Abstract `ToolBackend` with `lookup_order()`, `track_shipment()`, etc.
- `mock_backend.py` — Current hardcoded mock data (moved from registry.py)
- `stub_backend.py` — Configurable stub: reads from JSON fixtures in `data/fixtures/`
- `live_backend.py` — Real HTTP calls to OMS/WMS endpoints (credentials via env vars)
- `TOOL_BACKEND=mock|stub|live` env var selects active backend at startup

**3B-2: Order fixture generator** (`data/fixtures/`)
- Realistic Boohoo order fixtures: in_transit, processing, delivered, returned, cancelled
- Carrier tracking fixtures: Evri, DPD, Royal Mail, Hermes (realistic event sequences)
- Promo code fixtures with real conditions (min spend, brand exclusions, single-use)

**3B-3: Tool tests against stub backend**
- All 10 tools tested against stub fixtures
- Carrier tracking event parsing verified
- Error paths (OMS timeout, order not found) tested

---

## Phase 3C — Feedback Loop & Learning

### Goal
Close the loop: every resolved/escalated session feeds back into the knowledge graph
to improve future resolutions.

### Deliverables

**3C-1: Session outcome writer** (`core/feedback/outcome.py`)
- On session close: write outcome to `support_sessions` Postgres table
- Update `Resolution.success_rate` in FalkorDB using exponential moving average
- Tag failed resolutions with `escalation_reason` for review queue

**3C-2: Intent label queue processor** (`core/feedback/labeller.py`)
- For sessions with `confidence < 0.70`: write to `intent_label_queue`
- Async batch job: every hour, LLM re-labels the queue with correct intent
- Confirmed labels update the FalkorDB `IssueType` node's example pool

**3C-3: Policy change detector** (`ingestion/change_detector.py`)
- On each crawl: hash page content and compare to `crawl_sources.last_hash`
- If changed: trigger on-demand extraction for that URL only
- Write change event to Graphiti temporal KG (policy validity windows auto-updated)

**3C-4: Resolution effectiveness dashboard** (Grafana provisioned dashboard)
- Success rate per resolution per brand (from FalkorDB)
- Escalation rate by intent (from Prometheus)
- Average turns to resolution by issue category
- Customer satisfaction score distribution

---

## Phase 3D — Chatwoot Production Integration

### Goal
Full bidirectional Chatwoot integration: inbound messages routed to agents,
agent responses posted back, conversation metadata synced.

### Deliverables

**3D-1: Chatwoot client** (`channels/chat/chatwoot_client.py`)
- `post_message(conv_id, content, private=False)` — post agent reply
- `assign_team(conv_id, team_id)` — move to human team on escalation
- `add_label(conv_id, label)` — tag with intent for reporting
- `create_contact(name, email, phone)` — create/update contact record
- `get_conversation(conv_id)` — fetch conversation history for context

**3D-2: Brand → inbox mapping** (`channels/chat/inbox_router.py`)
- Maps Chatwoot inbox_id → brand_id (configured via env or DB)
- Supports multiple inboxes per brand (chat, email, WhatsApp)
- Fallback to default brand when inbox not mapped

**3D-3: Conversation state sync**
- Resolved sessions → Chatwoot conversation marked `resolved`
- Escalated sessions → Chatwoot conversation assigned to `L2 Support` team
- All tool results posted as private notes (visible to agents, not customers)

**3D-4: Webhook security**
- HMAC signature verification for Chatwoot webhook payloads
- Replay protection (timestamp-based nonce check)

---

## Phase 3E — n8n Escalation & Notification Workflows

### Goal
Automate everything that happens after a session closes or escalates,
without coupling it to the main API process.

### Deliverables

**3E-1: Escalation notification workflow**
- Trigger: `POST /webhook/n8n/escalation` from FastAPI on escalation
- Actions: Slack notification to L2 channel, create Jira/Linear ticket,
  email customer with ticket number and SLA, update Chatwoot conversation

**3E-2: Daily digest workflow** (cron: 08:00 UTC)
- Pull yesterday's session stats from Postgres
- Calculate resolution rate, avg turns, escalation %, CSAT
- Post digest to Slack #support-ops channel

**3E-3: Policy change alert workflow**
- Trigger: change_detector finds updated page
- Actions: Slack alert to brand manager, queue for human review,
  notify L2 agents of pending policy change

**3E-4: n8n workflow definitions** (`docker/n8n/workflows/`)
- JSON workflow definitions importable into n8n
- Environment-variable-driven (no hardcoded URLs)

---

## Phase 3F — Admin Review UI

### Goal
Human reviewers need a UI to approve/reject extracted knowledge items
sitting in the `resolution_review_queue`.

### Deliverables

**3F-1: Review queue API** (FastAPI, already partially wired)
- `GET /admin/review/queue` — paginated list of pending items with confidence scores
- `POST /admin/review/{item_id}/approve` — write to FalkorDB + clear queue entry
- `POST /admin/review/{item_id}/reject` — clear queue entry, log reason
- `POST /admin/review/{item_id}/edit` — edit content before approving

**3F-2: Simple review UI** (`admin/` directory, plain HTML + Alpine.js)
- No framework dependencies — just HTML served by FastAPI
- Card per queue item: source URL, extracted content, confidence bar, approve/reject buttons
- Filter by brand, item_type, date

---

## Phase 3G — Production Hardening

### Goal
The system must be safe to run in front of real customers.

### Deliverables

**3G-1: Auth & rate limiting**
- API key auth on all `/admin/*` and `/voice/*` endpoints
- Per-session rate limiting: max 30 turns/hour (Redis counter)
- Per-IP rate limiting on `/chat` (slowapi)

**3G-2: Input sanitisation**
- Reject messages > 2000 chars
- Strip HTML/script tags from all customer input
- Log and reject obvious prompt injection patterns

**3G-3: Observability**
- Structured JSON logging with trace_id propagated through all layers
- OTEL spans for: context assembly, KG query, tool call, LLM call, memory write
- Grafana dashboard: p50/p95/p99 latency, error rates, active sessions

**3G-4: Graceful degradation**
- FalkorDB unavailable → serve from Redis cache (30-min TTL on KG responses)
- LLM provider unavailable → retry with exponential backoff → fallback provider
- Redis unavailable → in-memory session (single-process only, warn loudly)

---

## Execution Order

```
3A (KB + E2E tests)   ← current sprint
3B (real tools)       ← next
3C (feedback loop)    ← parallel with 3B
3D (Chatwoot)         ← after 3B
3E (n8n workflows)    ← after 3D
3F (admin UI)         ← after 3C
3G (hardening)        ← final, before go-live
```

## Definition of Done (Phase 3)

- [ ] Boohoo knowledge base seeded: 60+ FAQs, real policies in FalkorDB + pgvector
- [ ] E2E tests pass: context engine assembles correct context for all 11 issue types
- [ ] Tool stub backend: all 10 tools work against fixture data
- [ ] Session feedback loop: outcomes update FalkorDB resolution success rates
- [ ] Chatwoot bidirectional: messages in, replies out, escalations assigned
- [ ] n8n: escalation → Slack in < 30 seconds
- [ ] Admin review UI: reviewers can approve/reject queue items
- [ ] Rate limiting + auth active on all admin/voice endpoints
- [ ] OTEL spans visible in Grafana for every request
- [ ] 0 P1 bugs in 48-hour staging soak
