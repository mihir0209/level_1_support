# How It Works — L1 Support AI Platform

A technical reference explaining every technology integrated into the platform, what role it plays, and how the pieces connect to each other. Useful for onboarding, demo prep, and architectural decision-making.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Data Collection Layer](#2-data-collection-layer)
3. [Knowledge Storage Layer](#3-knowledge-storage-layer)
4. [Three-Tier Memory System](#4-three-tier-memory-system)
5. [Chat Orchestration (LangGraph)](#5-chat-orchestration-langgraph)
6. [Context Engineering Engine](#6-context-engineering-engine)
7. [LLM Abstraction Layer](#7-llm-abstraction-layer)
8. [Agent Tools](#8-agent-tools)
9. [Document Ingestion](#9-document-ingestion)
10. [Dynamic Ontology System](#10-dynamic-ontology-system)
11. [API Layer](#11-api-layer)
12. [Observability](#12-observability)
13. [Voice Channel](#13-voice-channel)
14. [Data Flow — Full Request Lifecycle](#14-data-flow--full-request-lifecycle)
15. [Tips & Known Limitations](#15-tips--known-limitations)

---

## 1. System Overview

```
Brand signs up
     │
     ▼
[Data Collection] ──────────────────────────────────────┐
  crawl4ai BFS + stealth Playwright                      │
  trafilatura (text extraction)                          │
  ddgs (web search)                                      │
  FireCrawl (bot-bypass fallback)                        │
  doc_parser (PDF/DOCX/TXT uploads)                      │
     │                                                   │
     ▼                                                   ▼
[LLM Extraction] ──────► [Dynamic Ontology]       [pgvector / Postgres]
  LiteLLM → Anthropic/        FalkorDB               (semantic chunks)
  OpenAI/Azure                OntologyType
     │                        OntologyProposal
     ▼
[FalkorDB Knowledge Graph]
  Brand → FAQ, Policy, KnowledgeItem,
          SizeGuide, DeliveryOption, ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Customer sends message
     │
     ▼
[LangGraph Chat Pipeline]
  assemble_context ──► route_to_agent ──► generate_response
       │                                        │
  [Context Engine]                         [LLM call via
  Intent: Anthropic Haiku (fast)            LiteLLM]
  KB retrieval: FalkorDB                         │
  History: Graphiti (FalkorDB)                   ▼
  Session: Redis                        check_resolution
  Memory: ChromaDB MemPalace              │          │
                                       close     escalate
                                          │          │
                                     Graphiti    Graphiti
                                     (write)     (write)
```

Every brand's data is isolated by `brand_id` across all stores. There is no cross-brand data leakage by design.

---

## 2. Data Collection Layer

### crawl4ai
**File:** `ingestion/agent_collector.py` — `_stealth_crawl()`, `node_collect()`

crawl4ai provides the Playwright-based headless browser with stealth patches (fingerprint randomisation, JS injection, accordion expansion). Used as the **primary fetch strategy** for all web pages.

- `BFSDeepCrawlStrategy` drives the BFS traversal — starts at entry points, follows links round by round up to `_MAX_URLS_DEFAULT` (default 60) pages
- `FilterChain` + `URLPatternFilter` blocks `?color=`, `?variant=`, editorial paths (`/pages/the-*`, `/lookbook`, etc.) to avoid wasted crawl budget
- `DomainFilter` keeps BFS within the brand's own domain
- Soft timeout: 35s per page (logs `slow_page_fetch` warning). Hard timeout: 30 minutes per full collection run.

### trafilatura
**File:** `ingestion/agent_collector.py` — used inside `node_collect()` after each page fetch

trafilatura strips navigation, footers, sidebars, and boilerplate HTML, returning clean main-content text. This is what the LLM actually sees — not raw HTML. Its `include_tables=True` flag preserves size guides and comparison tables that would otherwise be flattened.

### ddgs (DuckDuckGo Search)
**File:** `ingestion/agent_collector.py` — `_ddg_search()`

Used by the orchestrator agent to **discover entry point URLs** for a brand — help-center pages, FAQ sections, return policy pages — before BFS begins. Wrapped with `asyncio.wait_for(timeout=15.0)` and HTML fallback. Failures are non-fatal: if DDG fails, the BFS still runs from whatever URLs the LLM generates directly.

### FireCrawl
**File:** `ingestion/agent_collector.py` — `_firecrawl_fetch()`

Cloud-based anti-bot bypass service. Used as **fetch strategy #2** in the waterfall (`stealth → firecrawl → google_cache → wayback → httpx`). Only activates when `FIRECRAWL_API_KEY` is set in `.env`. Best for Cloudflare/Akamai-protected brands where even stealth Playwright gets blocked.

### httpx + tenacity
**File:** `ingestion/agent_collector.py` — `_httpx_get()`

Bare async HTTP client used as the **last-resort fallback** for unprotected pages and for probing (`probe_site_access()`). Wrapped with `@retry(stop_after_attempt(3), wait_exponential(min=2, max=30))` for `TimeoutException` and `ConnectError`.

### Fetch strategy waterfall

```
stealth (Playwright)
    │ fails or bot-gated?
    ▼
FireCrawl (if API key set)
    │ fails?
    ▼
Google Cache (cache.google.com/search?q=cache:URL)
    │ fails?
    ▼
Wayback Machine (web.archive.org/web/2*/URL)
    │ fails?
    ▼
plain httpx with browser User-Agent
```

---

## 3. Knowledge Storage Layer

### FalkorDB (l1_support graph)
**Files:** `core/ontology/schema.py`, `core/knowledge/retriever.py`, `ingestion/agent_collector.py`

FalkorDB is a Redis-native property graph database that speaks Cypher. All brand knowledge lives here as typed nodes and relationships.

**Node types and their sources:**

| Node label | Source | Key properties |
|---|---|---|
| `Brand` | seed / brand registration | `id`, `name`, `tier`, `voice_persona` |
| `FAQ` | crawl / document upload | `question`, `answer`, `category`, `keywords` |
| `Policy` | crawl / upload | `policy_type`, `title`, `content` |
| `ReturnPolicy` | seed data | `return_window_days`, `return_cost_gbp`, `free_return_condition` |
| `ShippingPolicy` | seed data | `standard_days_min`, `standard_cost`, `free_shipping_threshold` |
| `KnowledgeItem` | crawl (generic fallback) | `title`, `content`, `chunk_type`, `url` |
| `SizeGuide` | crawl (ontology-proposed) | `title`, `content`, `measurement_unit` |
| `DeliveryOption` | crawl (ontology-proposed) | `title`, `content`, `category` |
| `ReturnMethod` | crawl (ontology-proposed) | `title`, `content` |
| `IssueType` | seed data | `id`, `category` |
| `Resolution` | seed data | `name`, `steps`, `success_rate` |
| `OntologyType` | dynamic (agent-proposed) | `label`, `properties`, `relationships` |
| `OntologyProposal` | dynamic | `label`, `status`, `evidence` |
| `Customer` | CRM / demo data | `id`, `tier`, `brand_id` |
| `Order` | CRM / demo data | `id`, `status`, `placed_at` |

**Why FalkorDB instead of Neo4j?** Redis-native = single infrastructure component (same host as session cache), sub-millisecond query latency, no JVM overhead.

### PostgreSQL + pgvector
**File:** `ingestion/agent_collector.py` — `_write_pgvector()`, `main.py` — `/admin/kb/chunks`

Every extracted KB item is also stored as a text embedding in the `kb_chunks` table. Columns: `brand_id`, `entity_type`, `title`, `content`, `embedding` (vector), `source_url`, `crawled_at`, `updated_at`.

Currently used for: the `/admin/kb/chunks` API (semantic search preview). Future: full vector similarity fallback in the retriever when keyword search returns nothing.

**Schema note:** The table uses `crawled_at`/`updated_at` (not `created_at`). This burned us once — documented here so it doesn't again.

---

## 4. Three-Tier Memory System

The system uses three memory stores at different time horizons.

### Tier 1: Redis — Working Memory
**File:** `core/memory/session.py` — `SessionMemory`, `get_redis()`

- **What:** In-session state — messages, turn count, intent, entities, resolution attempts
- **TTL:** 2 hours of inactivity
- **Key pattern:** `session:{uuid4}`
- **Value:** JSON blob of the full session state dict
- **Singleton:** `get_redis()` returns one shared `aioredis.from_url()` connection pool per process. Creating a new pool per call (the original bug) caused `ResourceWarning: unclosed Connection` on every request.

### Tier 2: ChromaDB — MemPalace (30-day session storage)
**File:** `core/memory/mempalace_client.py` — `MemPalaceClient`

- **What:** Compressed session transcripts stored for 30 days
- **Collection naming:** `mp_{brand_id}` — one ChromaDB collection per brand
- **Retrieval:** Cosine similarity search (ChromaDB's default) filtered by `customer_id` and `session_id` metadata
- **Purpose:** "This customer called last week about a missing parcel" context, retrievable in a new session
- **Storage:** Local persistent directory at `./data/mempalace` (configurable via `MEMPALACE_DATA_DIR`)

### Tier 3: FalkorDB (l1_user_memory) — Graphiti Temporal KG
**Files:** `core/memory/graphiti_client.py`, `core/orchestration/graph.py`

Graphiti is a temporal knowledge graph library (`graphiti-core`) that stores facts as **edges with valid/invalid timestamps** — it knows that a fact was true in April but not in May, without you having to manage that explicitly.

**What gets written:** At session close or escalation, `_build_episode_text()` constructs a natural-language summary:
```
Customer contacted support via chat about return_initiate (return).
Session lasted 3 turn(s). Outcome: resolved.
Order referenced: BH-12345-UK. Return reason: wrong size.
Resolution applied: return_initiate.
```
Graphiti extracts entities and relationships from this text and stores them as graph edges timestamped to now.

**What gets read:** On every new chat turn, `graphiti_memory.search()` runs in parallel with knowledge retrieval. It returns fact strings like:
- "Customer returned a dress in March due to sizing issue"
- "Customer previously escalated a payment dispute"

These are injected as `PAST INTERACTIONS (Graphiti memory):` in the agent's system prompt.

**Group ID isolation:** Each brand×customer pair uses `group_id = "{brand_id}:{customer_id}"`. FalkorDB graph name: `l1_user_memory` (separate from `l1_support`).

**Embedder note:** Graphiti needs an embedder for semantic search. When using Anthropic as LLM provider, you need an `OPENAI_API_KEY` set for embeddings (Anthropic has no embeddings API). Azure provider uses its own embedder deployment.

---

## 5. Chat Orchestration (LangGraph)

**File:** `core/orchestration/graph.py`

LangGraph compiles a `StateGraph` into a DAG of async nodes. The state (`L1SupportState`) is a Pydantic model that persists across nodes in a single turn.

```
START
  │
  ▼
assemble_context   ← runs on every turn; full 8-step context pipeline
  │
  ▼
route_to_agent     ← maps intent → specialist agent name (no real Swarms yet)
  │
  ▼
generate_response  ← main LLM call with full ContextPackage in system prompt
  │
  ▼
check_resolution   ← conditional edge
  ├── "tools"    → ToolNode (execute tool calls) → back to generate_response
  ├── "escalate" → node_escalate → Graphiti write → END
  ├── "close"    → node_close_session → Graphiti write + feedback loop → END
  └── "continue" → END (wait for next customer message)
```

**Escalation thresholds:**
- `resolution_attempts >= 10`
- `turn_count >= 10`
- `confidence < 0.45` after turn 1
- Agent response contains explicit escalation phrase ("connect you with", "human agent", etc.)
- `[RESOLVED]` signal in response → close

**ToolNode:** LangGraph's built-in tool executor. When the LLM returns a response with `tool_calls`, ToolNode runs them, appends `ToolMessage` results to state, then routes back to `generate_response`. The LLM sees tool results and produces the final answer.

**Ingestion pipeline** (separate LangGraph in `agent_collector.py`):
```
START → orchestrate → collect → ontology_review → synthesize → END
```
This runs as a background task triggered by `POST /admin/brands/{id}/collect`.

---

## 6. Context Engineering Engine

**File:** `core/context/engine.py`

Runs before every LLM call. Returns a `ContextPackage` used to build the system prompt. Eight steps, total target latency ~325ms:

| Step | What | How |
|---|---|---|
| 1 | Intent classification | Haiku call, returns `issue_type` slug + confidence |
| 2 | Entity extraction | Part of the same classification JSON response |
| 3 | User context | Passed in from session (customer object from Redis/CRM) |
| 4 | KB retrieval (Pattern A) | FalkorDB `IssueType→Resolution` traversal |
| 4C | KB fallback (Pattern C) | `search_kb_nodes()` — keyword CONTAINS on FAQ/KnowledgeItem/DeliveryOption when Pattern A finds nothing |
| 5 | Policy retrieval | Temporal return/shipping policy lookup (effectiveDate aware) |
| 6 | Session compression | Sliding window — last 4 turns verbatim + older turns summarised inline |
| 7 | Tool manifest | Intent → tool list lookup table |
| 8 | Context packing | Format all blocks into system prompt text |

Steps 4, 5, and Graphiti history (Tier 3 read) run in a single `asyncio.gather()` — parallel, no stacking.

**Confidence score:** `max(intent.confidence, knowledge.retrieval_confidence)`. Using only intent confidence caused false escalations when NLU classified something as "unknown" but KB search found highly relevant content.

**Delivery intent fix:** Delivery queries used to classify as `unknown` and get empty resolutions. Added `delivery_query` as a proper intent type with `policy_lookup + shipment_track` tools and `wismo_agent` routing.

---

## 7. LLM Abstraction Layer

**File:** `core/orchestration/llm.py`

**LiteLLM** (`litellm==1.76.1` pinned — later versions have breaking Azure serialisation issues) provides a unified API surface across providers. All LLM calls go through `LLMClient.generate()` or `LLMClient.get_lc_client()`.

**Provider routing** (set via `LLM_PROVIDER` env var):

| Provider | Primary model | Fast model | Embeddings |
|---|---|---|---|
| `anthropic` | `claude-sonnet-4-6` | `claude-haiku-4-5` | OpenAI (requires `OPENAI_API_KEY`) |
| `openai` | `gpt-4o` | `gpt-4o-mini` | OpenAI native |
| `azure` | deployment name | deployment name | Azure embedding deployment |

**Two model tiers:**
- **Fast model** (Haiku / gpt-4o-mini): intent classification, entity extraction — low latency, low cost
- **Primary model** (Sonnet / gpt-4o): response generation, LLM extraction during crawl

**LangChain wrappers** (`langchain-anthropic`, `langchain-openai`) are used in `generate_response` because LangGraph's `ToolNode` needs LangChain-compatible message objects. The LiteLLM path is used for everything that doesn't touch tools.

---

## 8. Agent Tools

**File:** `core/tools/registry.py`

Tools follow the `@tool` decorator pattern from `langchain_core.tools`. LangGraph's `ToolNode` calls them when the LLM issues a tool call in its response.

**Current tools (Phase 1 — mock data):**

| Tool | What it does |
|---|---|
| `order_lookup` | Returns order status, items, carrier by order ID |
| `shipment_track` | Returns tracking URL + estimated delivery |
| `return_init` | Initiates a return — returns case ID |
| `refund_status` | Returns refund timeline |
| `policy_lookup` | Fetches return/shipping policy for brand |
| `size_guide` | Returns size conversion table for brand |
| `catalog_lookup` | Checks if a product is in stock |
| `cancel_order` | Cancels an order if still in processing |
| `account_status` | Returns account locked/active status |
| `promo_validate` | Validates a promo code |
| `payment_lookup` | Returns payment method + transaction status |
| `escalate` | Creates a support ticket (mock Chatwoot) |

**Phase 2:** Replace mock implementations with real brand API calls. The tool signatures stay identical — only the body changes.

---

## 9. Document Ingestion

**Files:** `ingestion/doc_parser.py`, `ingestion/agent_collector.py` — `collect_from_documents()`

Brands or FDEs can upload knowledge documents directly instead of (or alongside) web crawling.

**Parsing pipeline:**

| Format | Library | Notes |
|---|---|---|
| `.pdf` | PyMuPDF (`fitz`) | Per-page text extraction; preserves tables |
| `.docx`, `.doc` | python-docx | Section-aware: groups paragraphs under headings before chunking |
| `.txt`, `.md` | built-in | Direct read |
| `.html`, `.htm` | trafilatura | Same as web crawl text extraction |

All formats produce `{text, source, page}` chunks of ≤6000 characters with 300-character overlap between consecutive chunks from the same section.

**After parsing:** each chunk goes through the exact same `_extract_chunk()` LLM call used for web pages — same prompt, same ontology proposals, same `_write_graph()` + `_write_pgvector()` writes. Uploaded documents and crawled pages are indistinguishable in the knowledge graph.

**Upload endpoint:** `POST /admin/brands/{brand_id}/documents/upload` — multipart form, `X-API-KEY` required. Files land in a `tempfile.TemporaryDirectory()` and are cleaned up after processing.

---

## 10. Dynamic Ontology System

**File:** `core/ontology/dynamic.py`

The web crawl pipeline doesn't know in advance what entity types a brand's website will contain. The ontology system lets the LLM **propose new types during extraction** and adds them to the schema on the fly.

**Three-way decision (node_ontology_review):**

| Decision | When | Action |
|---|---|---|
| `APPROVED` | New type with real evidence | `propose_entity_type()` → creates `OntologyType` node |
| `MERGED` | Label exists, new properties found | `merge_entity_type()` → unions properties + relationships |
| `REJECTED` | Alias, vague, or no direct evidence | Discarded |

**Node label alignment:** Custom entity types are stored with the type as the actual Cypher node label (`:SizeGuide`, `:DeliveryOption`), not as `:KnowledgeItem {entity_type: "SizeGuide"}`. This makes Cypher queries clean and allows label-specific indexes.

**`_valid_node_label()`** in agent_collector.py validates that an entity type string is a legal Cypher identifier before using it as a label — prevents injection and falls back to `KnowledgeItem` for malformed values.

**Migration:** If old data with wrong labels exists, `POST /admin/ontology/migrate-nodes?dry_run=true` shows what would be changed; `dry_run=false` fixes it.

---

## 11. API Layer

**File:** `main.py`

**FastAPI** with async lifespan. On startup: FalkorDB connectivity check → schema creation → seed data load → APScheduler start → dynamic brand registry restore from Redis.

**Router structure:**

| Path prefix | Tags | Auth |
|---|---|---|
| `/session/*`, `/chat` | Chat | None |
| `/graph/*` | Graph | None |
| `/kb/*` | Knowledge Base | None |
| `/brands/*` | Brands | None |
| `/admin/*` | Operations, System | `X-API-KEY` header |
| `/voice/*` | Voice | None |
| `/metrics` | System | None |

**APScheduler** (`apscheduler>=3.10.0`): Runs periodic re-crawls for registered brands. Defined in `ingestion/scheduler.py`. Jobs are stored in memory — restart clears the schedule (acceptable for Phase 1).

**Brand registry** (`brands/registry.py`): In-memory dict of `BrandEntry` objects. Dynamic registrations (via `POST /admin/brands`) are persisted to Redis hash `brand_registry` and restored at startup. The 13 Boohoo Group brands are hardcoded; new brands are registered at runtime.

**Prometheus metrics** (via `prometheus-client`):
- `l1_messages_total` — by brand + channel
- `l1_resolution_latency_seconds` — histogram, per turn
- `l1_escalations_total` — by brand + reason
- `l1_sessions_total` — by brand + channel

---

## 12. Observability

**structlog** (`structlog>=24.4.0`): All log lines are structured JSON objects with consistent fields (`brand_id`, `session_id`, `latency_ms`, etc.). No `print()` statements — everything goes through `log.info()` / `log.warning()` / `log.error()`.

**LangSmith**: Optional tracing for LangChain calls. Set `LANGCHAIN_TRACING_V2=true` and `LANGSMITH_API_KEY` in `.env`. Traces every LLM call with inputs/outputs — invaluable for debugging prompt failures.

**OpenTelemetry**: SDK configured, OTLP exporter pointed at `OTEL_EXPORTER_OTLP_ENDPOINT` (default localhost:4317). Not yet deeply instrumented but the SDK is present for Phase 2.

**Noise suppression** (in `main.py` at import time):
- `warnings.filterwarnings("ignore", message="Pydantic serializer warnings")` — LiteLLM/Azure response shape mismatch with internal Pydantic models
- `warnings.filterwarnings("ignore", message=".*renamed to.*ddgs.*")` — `duckduckgo-search` → `ddgs` package rename warning

---

## 13. Voice Channel

**Files:** `channels/voice/`, `main.py` — `/voice/*`

Phase 3 — defined but not fully integrated. The stack:

| Component | Library | Role |
|---|---|---|
| Telephony | Twilio (`twilio>=9.3.0`) | PSTN inbound calls → webhook |
| Real-time transport | Daily (`pipecat-ai[daily]`) | WebRTC room for voice session |
| STT | Deepgram (`pipecat-ai[deepgram]`) | Speech-to-text, streaming |
| TTS | ElevenLabs (`pipecat-ai[elevenlabs]`) | Text-to-speech, voice cloning per brand |
| LLM | Anthropic via pipecat | Same Claude models, voice-optimised prompts |
| VAD | Silero (`pipecat-ai[silero]`) | Voice activity detection (end-of-speech detection) |

The voice channel uses **pipecat-ai** as the pipeline framework. A voice session runs as a pipeline: microphone → Silero VAD → Deepgram STT → LangGraph (same chat graph) → ElevenLabs TTS → speaker.

Voice-specific formatting rules are injected via `_CHANNEL_ADDITIONS[Channel.VOICE]` in the context engine: max 2 sentences, no markdown, spell out numbers, no bullet points.

---

## 14. Data Flow — Full Request Lifecycle

### A. Brand onboarding (one-time)

```
POST /admin/brands/register
  → BRAND_REGISTRY.register() + Redis hash persist
  → POST /admin/brands/{id}/collect (background task)
      → node_orchestrate: DDG search + LLM URL generation → entry_points[]
      → node_collect: BFS with crawl4ai, trafilatura, _extract_chunk per page
          → each page: LLM → {items[], ontology_proposals[]}
      → node_ontology_review: deduplicate + APPROVE/MERGE/REJECT proposals
      → node_synthesize: _apply_proposals + _write_graph + _write_pgvector
```

### B. Customer chat turn

```
POST /chat  {session_id, message}
  → load session from Redis
  → LangGraph.ainvoke(state + new HumanMessage)
      → assemble_context:
          ├── classify_intent (Haiku, ~50ms)
          ├── retrieve_knowledge (FalkorDB, ~80ms)  ─┐
          ├── get_brand_info (FalkorDB, ~10ms)        ├─ asyncio.gather
          └── graphiti_memory.search (FalkorDB, ~100ms)─┘
          → build ContextPackage (system prompt + 5 blocks)
      → route_to_agent (intent → agent name map, ~0ms)
      → generate_response (Sonnet/GPT-4o, ~800ms–2s)
          → if tool_calls: ToolNode → re-generate
      → check_resolution → continue | escalate | close
          → close/escalate: graphiti_memory.add_episode (async, non-blocking)
  → save session to Redis
  → return AI response text
```

### C. Document upload

```
POST /admin/brands/{id}/documents/upload  (multipart)
  → save files to tempdir
  → collect_from_documents():
      → parse_files(): fitz/docx/trafilatura → [{text, source, page}]
      → _extract_chunk() × N chunks (batches of 4)
          → LLM → {items[], ontology_proposals[]}
      → _apply_proposals() → OntologyType nodes
      → _write_graph() + _write_pgvector()
  → return extraction counts
```

---

## 15. Tips & Known Limitations

### Things to know before going live

**1. Graphiti requires an embedder for semantic search.**
With `LLM_PROVIDER=anthropic`, Graphiti's `search()` silently returns empty results unless `OPENAI_API_KEY` is also set (Anthropic has no embeddings endpoint). Either set `OPENAI_API_KEY` for embeddings only, or switch to `LLM_PROVIDER=azure`/`openai` which have native embedders.

**2. FalkorDB is Redis-native — it runs on the same port (6379) as the session cache by default.**
Redis session cache is on port **6380** (`REDIS_URL=redis://localhost:6380/0`) and FalkorDB on **6379** (`FALKORDB_PORT=6379`). These are separate Docker containers. The ports look confusingly similar — check `docker-compose.yml` if sessions aren't persisting.

**3. litellm is pinned at 1.76.1.**
Later versions have a breaking import in `litellm/main.py` that tries to instantiate `VertexImageGeneration` at module load, which fails without GCP credentials. Do not upgrade without testing the full import chain. The pin is intentional and documented in commit history.

**4. pgvector table has `crawled_at`/`updated_at`, not `created_at`.**
The `kb_chunks` table was created by an early migration with these column names. Any query using `ORDER BY created_at` will fail silently or error. Use `crawled_at` for time-based ordering.

**5. IssueType seed data is global (no brand_id).**
`IssueType` and `Resolution` nodes are seeded once and apply to all brands. When adding a new brand whose resolution paths differ significantly (e.g., a brand that doesn't accept returns), you need brand-specific `IssueType` nodes. The `where it.brand_id IS NULL OR it.brand_id = $brand_id` clause in `get_resolutions_for_issue()` supports this — just seed the brand-specific node.

**6. The BFS editorial block list may need per-brand tuning.**
`_SKIP_PATH_FRAGMENTS` in `agent_collector.py` blocks common editorial/lifestyle paths. Brands on custom CMS platforms may use different URL patterns. If a collection run returns 0 items, check the BFS logs for the visited URLs and add the relevant skip patterns.

**7. ChromaDB MemPalace collections survive `reset_db.py --brand-id` but not `--full`.**
Use `scripts/reset_db.py --brand-id {id}` to wipe a single brand cleanly across all five stores before a re-ingestion demo.

**8. `POST /admin/ontology/migrate-nodes` is needed after first deploy.**
If data was collected before the `_valid_node_label()` fix was deployed, ~139 custom-typed nodes may be stored as `:KnowledgeItem {entity_type: "SizeGuide"}` instead of `:SizeGuide`. Run `POST /admin/ontology/migrate-nodes?dry_run=true` to see the count, then `dry_run=false` to fix.

### Architecture tips for Phase 2

- **Replace mock tools with real API calls** by updating only the function bodies in `core/tools/registry.py`. The tool signatures, `@tool` decorators, and LangGraph wiring don't change.
- **Chatwoot escalation** is currently a log line. Wire `node_escalate` to `POST /api/v1/accounts/{id}/conversations` with the full context package as the conversation note. The context package structure already matches what a human agent needs.
- **Multi-turn memory** currently relies on Redis session state (2-hour TTL). For email channels where a customer replies days later, promote the Graphiti search results to first-class context and extend the session TTL or use a persistent session store.
- **Rate limiting** on `POST /chat` is not implemented. Add a Redis-based sliding window rate limiter before exposing to public traffic.
- **Embedding quality matters for KB search.** The current pgvector rows store zero-vector placeholders unless `EMBED=true` is set during seeding. For production, run the seeder with a real embedding model and rebuild the vector index.
