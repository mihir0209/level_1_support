# L1 Support AI — Full Research Synthesis
*Generated: 2026-04-16 | Status: Pre-planning artifact*

---

## 1. PROBLEM DEFINITION

L1 Support = first-line, high-volume, entry-level resolution via voice/chat/email/social.
- Indian salary context: Rs. 30,000–50,000/year per technician
- 80% of queries across telecom, e-commerce, consumer electronics, gaming are repetitive
- Zero downtime = 24/7 availability requirement
- Resolution velocity matters more than depth

**The four target domains:**
| Domain | Query Type | Primary Channel | Key Data |
|---|---|---|---|
| Telecom | Account, billing, connectivity | Voice + Chat | Account DB, network status |
| Consumer Electronics | Hardware troubleshooting | Chat + Email | Device manuals, firmware KB |
| E-commerce | Orders, returns, FAQs | Chat + Email | Order DB, product catalog |
| Video Games | FAQs, account issues | Chat + Social | Game KB, account DB |

---

## 2. OPEN SOURCE TOOLS — CONFIRMED FINDINGS

### A. Multi-Agent Orchestration

#### Swarms (`kyegomez/swarms`)
- **License:** Apache 2.0 | **Install:** `pip install swarms`
- **Core patterns:**
  - `SequentialWorkflow` — linear agent chaining
  - `HierarchicalSwarm` — supervisor/worker pattern (best for L1 triage)
  - `MixtureOfAgents` — parallel experts + synthesizer
  - `SwarmRouter` — runtime strategy selection
  - `AutoSwarmBuilder` — auto-generate agent teams from description
- **LLM support:** OpenAI, Anthropic, Groq, Ollama, DeepSeek, Cohere
- **MCP support:** Yes — dynamic tool discovery
- **L1 fit:** HierarchicalSwarm maps perfectly to: triage agent → domain specialist → escalation agent

#### LangGraph (`langchain-ai/langgraph`)
- **License:** MIT | v1.0 stable since Oct 2025
- **Why it matters:** State machine for multi-turn conversations with persistence
- **400+ companies in production** including Klarna, Replit, Elastic
- Nodes = steps (classify → retrieve → respond → escalate)
- Edges = conditional control flow
- Built-in human-in-the-loop (pause and wait for agent approval)
- LangSmith for observability
- **Best for:** stateful support dialog management per user session

---

### B. Knowledge Graph & Memory

#### Graphiti (`getzep/graphiti`)
- **License:** Apache 2.0 | `pip install graphiti-core`
- **Key differentiator:** BI-TEMPORAL tracking — facts have validity windows, history preserved
- **Backends:** Neo4j 5.26+, FalkorDB, Kuzu, Amazon Neptune
- **Retrieval:** Semantic + BM25 keyword + graph traversal (hybrid)
- **Latency:** Sub-second
- **MCP server:** Included — ready for Claude/Cursor integration
- **Relationship to Zep:** Graphiti is the OSS core; Zep is the commercial wrapper
- **L1 fit:** Perfect for "customer reported this issue 3 weeks ago, here's what fixed it"

#### Cognee (`topoteretes/cognee`)
- **License:** Apache 2.0 | Python 3.10–3.13
- **APIs:** `remember()`, `recall()`, `forget()`, `improve()`
- **Architecture:** Vector search + Graph DB + Session cache (auto-routed)
- **Deployment:** Cognee Cloud, Modal, Railway, Fly.io, Render
- **Observability:** OpenTelemetry + audit trails
- **L1 fit:** Agent memory across sessions; per-user context retention

#### MemPalace (`MemPalace/mempalace`)
- **License:** MIT | v3.3.0 | 47,000 GitHub stars
- **Key feature:** VERBATIM storage + semantic retrieval (no summarization)
- **96.6% R@5 recall WITHOUT LLM calls**
- **Architecture:** Wings → Rooms → Drawers hierarchy; SQLite + ChromaDB
- **29 MCP tools** for integration
- **Requirements:** Python 3.9+, ~300 MB disk
- **L1 fit:** Local-first, no data leaves server — critical for compliance

#### Microsoft GraphRAG
- **GitHub:** `microsoft/graphrag`
- **Use case:** BOOTSTRAP only — run once over historical ticket corpus to extract knowledge
- **NOT for live queries** — expensive re-index, not real-time
- **Cost:** $50–200 in LLM API calls per 10K doc re-index

---

### C. Voice AI Pipeline

#### Pipecat (`pipecat-ai/pipecat`)
- **License:** BSD-2-Clause | Python 3.11+ (3.12 recommended)
- **Architecture:** Frame-based streaming pipeline (AudioFrame → STT → LLM → TTS → AudioFrame)
- **STT:** Deepgram, AssemblyAI, Whisper, Azure, Google, Groq, ElevenLabs, 10+ more
- **TTS:** OpenAI, ElevenLabs, Cartesia, Deepgram Aura, Azure, Google, NVIDIA Riva, 20+ more
- **Transport:** Daily (WebRTC), LiveKit, WhatsApp, Twilio (SIP), WebSocket
- **Telephony:** Twilio SIP trunking to WebRTC bridge
- **Observability:** OpenTelemetry + Sentry
- **L1 fit:** Full inbound/outbound voice call handling; swap STT/TTS without restructuring

#### Bolna (`bolna-ai/bolna`)
- **License:** MIT core (hosted platform closed)
- **Architecture:** Transcriber → LLM Agent → Synthesizer (WebSocket-based)
- **Telephony:** Twilio, Plivo
- **STT:** Deepgram, Azure
- **LLM:** 15+ via LiteLLM (OpenAI, Anthropic, DeepSeek, Llama, etc.)
- **TTS:** ElevenLabs, Cartesia, Deepgram, AWS Polly, Smallest
- **Latency:** <300ms response
- **Pricing (cloud):** $0.02/min platform + ~$0.04/min provider costs
- **L1 fit:** Simpler than Pipecat; faster to deploy for pure voice; India-optimized

---

### D. Frontend / Ticketing / Omnichannel

#### Chatwoot
- **License:** MIT | Self-hosted or cloud
- **Channels:** Email, live chat, WhatsApp, Facebook, Instagram, Twitter, Telegram
- **AI:** "Captain" assistant (summarize, suggest replies, deflect tickets)
- **Deploy:** Docker Compose (2–4 hours), needs 4GB RAM
- **L1 fit:** Best OSS alternative to Zendesk/Intercom; unified inbox for L1 agents

#### Tiledesk
- **License:** Apache 2.0 | Open source
- **Feature:** No-code LLM agent builder + HITL (human-in-the-loop)
- **Positioned as:** OSS alternative to Voiceflow
- **L1 fit:** Quick agent building without code; best OSS no-code solution

---

### E. Supporting Infrastructure

| Component | Tool | License |
|---|---|---|
| Graph Database | Neo4j (AGPL/Enterprise) or FalkorDB (Redis-based) | Mixed |
| Vector Search | pgvector (Postgres ext) or Qdrant | Apache 2.0 / MIT |
| Session Cache | Redis | BSD |
| Workflow Automation | n8n (self-hostable) | Apache 2.0 |
| Ontology Authoring | Protégé (Stanford) | LGPL |
| RDF/OWL | RDFLib (Python) | BSD |
| Telecom Standard | TM Forum SID (OpenAPI specs) | Open |
| KG Observability | LangSmith | Closed/SaaS |
| KG Ingestion Pipeline | Microsoft GraphRAG (bootstrap) | MIT |

---

## 3. COMPETITOR LANDSCAPE

### Cognigy AI (Heaviest Competitor)
- **Price:** $50K–$2M+/year, enterprise only, no self-serve
- **Strengths:** Best telephony integration (Genesys, Avaya, Cisco), real-time voice, CCaaS-grade
- **Weaknesses:** Prohibitive cost, vendor lock-in, no BYOM, no community/plugins
- **OSS gap to exploit:** Price, on-prem, model choice, community

### Zendesk AI
- **Price:** $150–250/agent/month all-in (base + AI add-on)
- **Strengths:** 160K customers installed base, email triage best-in-class
- **Weaknesses:** Weak voice AI, locked to OpenAI model, no on-prem, proprietary schema
- **OSS gap:** BYOM, data sovereignty, cost, voice

### Bolna AI (Cloud)
- **Price:** $0.02/min platform + ~$0.04/min provider costs
- **Strengths:** Voice-first, India-language optimized, API-driven
- **Weaknesses:** Voice only, no KB/ticketing, no analytics, not truly OSS anymore

### Haptik (Jio Haptik)
- **Price:** $30K–200K/year enterprise
- **Strengths:** WhatsApp excellence, South Asian languages, 15B+ conversations
- **Weaknesses:** Asia-centric, limited voice at enterprise level, no BYOM, no on-prem

### Nurix AI (not Nutrix)
- **Focus:** Voice-first, real-time speech, end-to-end resolution
- **Gap:** Proprietary, expensive, enterprise only

### THE KEY GAP (Market Opportunity)
**No single OSS solution combines:**
- ✗ Voice + Chat + Email in one unified system
- ✗ Knowledge graph (not just flat RAG)
- ✗ Per-user memory across sessions
- ✗ Multi-tenant (sell to multiple enterprise clients)
- ✗ On-premises deployable
- ✗ BYOM (plug in any LLM)
- ✗ Human-in-the-loop agent assist
- ✗ Analytics dashboard

**This is the architectural opportunity.**

---

## 4. TECHNICAL DECISIONS (Pre-resolved)

### Decision 1: Graph Storage
**Choose:** Property graph (Neo4j or FalkorDB) over RDF/OWL
- Reason: LLMs generate Cypher reliably; operational support = traversal problems not class-inference
- Exception: If telecom client requires TM Forum SID compliance → expose TM Forum API on top of Neo4j

### Decision 2: RAG Strategy
**Choose:** Local GraphRAG (hybrid: vector + graph traversal) over naive RAG
- 77.6% better retrieval accuracy (LinkedIn SIGIR 2024)
- 28.6% reduction in resolution time
- Engineering cost: 2-3x naive RAG — justified for production accuracy

### Decision 3: Agent Memory Architecture
**Two-tier approach:**
- Session context (within call/chat): LangGraph state + Redis (TTL = session)
- Long-term user history: Graphiti temporal KG (persisted, queryable)

### Decision 4: LLM-to-Graph Interface
**Hybrid approach:**
- Pre-built Cypher tool functions for 80% of predictable L1 queries (safe, fast)
- Text2Cypher as fallback for novel queries (read-only Neo4j user, injection mitigation)

### Decision 5: Voice Stack
**Primary:** Pipecat (more composable, broader STT/TTS choice)
**Bootstrap alternative:** Bolna (faster POC for voice-only path)
**Telephony:** Twilio or Plivo (SIP → WebRTC bridge via Daily.co or LiveKit)

### Decision 6: Orchestration
**Primary:** LangGraph (stateful, production-tested, HITL built-in)
**Multi-agent layer:** Swarms HierarchicalSwarm for domain routing
- Supervisor agent: triage + intent classification
- Specialist agents: one per domain (telecom, e-commerce, gaming, consumer electronics)
- Escalation agent: ticket creation + human handoff

---

## 5. PROPOSED SYSTEM ARCHITECTURE (Conceptual)

```
┌─────────────────────────────────────────────────────────────┐
│                    CHANNEL LAYER                             │
│  Voice (Pipecat)  Chat (Chatwoot)  Email (Chatwoot)  Social │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                 ORCHESTRATION LAYER                          │
│     LangGraph (stateful session)                             │
│     ┌──────────────────────────────────┐                     │
│     │ classify → retrieve → respond    │                     │
│     │ → escalate_or_close             │                     │
│     └──────────────────────────────────┘                     │
│     Swarms HierarchicalSwarm (domain routing)               │
└─────────────────────┬───────────────────────────────────────┘
                      │
         ┌────────────┼────────────┐
         │            │            │
┌────────▼────┐ ┌─────▼─────┐ ┌──▼──────────┐
│ KNOWLEDGE   │ │  USER     │ │   TOOLS     │
│ GRAPH LAYER │ │ CONTEXT   │ │   LAYER     │
│             │ │  LAYER    │ │             │
│ Neo4j /     │ │ Graphiti  │ │ CRM lookup  │
│ FalkorDB    │ │ (temporal │ │ Ticket API  │
│             │ │ KG)       │ │ Order DB    │
│ GraphRAG    │ │           │ │ Account API │
│ pipeline    │ │ Cognee /  │ │ Password    │
│ neo4j-      │ │ MemPalace │ │ reset       │
│ graphrag    │ │ (session  │ │             │
│             │ │ memory)   │ │ n8n         │
└─────────────┘ └───────────┘ │ (workflow   │
                               │ automation) │
                               └─────────────┘
         │
┌────────▼───────────────────────────────────────┐
│              LLM LAYER (BYOM)                   │
│  Claude Sonnet / GPT-4o / Llama 3 / Gemini      │
│  via LiteLLM (provider abstraction)             │
└────────────────────────────────────────────────┘
         │
┌────────▼───────────────────────────────────────┐
│           KNOWLEDGE INGESTION PIPELINE          │
│  Microsoft GraphRAG → bootstrap from KB docs   │
│  Nightly batch → NER/RE → Neo4j upsert         │
│  Real-time Kafka path → incident propagation   │
└────────────────────────────────────────────────┘
         │
┌────────▼───────────────────────────────────────┐
│            AGENT UI (Human-in-loop)             │
│  Chatwoot (MIT) — unified inbox                 │
│  Tiledesk — no-code agent builder               │
└────────────────────────────────────────────────┘
```

---

## 6. TECHNOLOGY STACK SELECTION (FOSS-first)

| Layer | Primary Choice | Fallback | License |
|---|---|---|---|
| Voice pipeline | Pipecat | Bolna | BSD-2 / MIT |
| Telephony | Twilio (API) | Plivo | Commercial API |
| STT | Deepgram (API) | faster-whisper | Commercial / MIT |
| TTS | ElevenLabs or Cartesia | Coqui TTS | Commercial / MIT |
| Chat frontend | Chatwoot | Tiledesk | MIT / Apache 2.0 |
| Agent orchestration | LangGraph | Swarms | MIT / Apache 2.0 |
| Multi-agent patterns | Swarms HierarchicalSwarm | CrewAI | Apache 2.0 |
| Knowledge graph | Neo4j (AuraDB free tier) → FalkorDB | Memgraph | AGPL / Apache 2.0 |
| GraphRAG pipeline | neo4j-graphrag-python | LlamaIndex PropertyGraphIndex | Apache 2.0 |
| Agent memory | Graphiti | Cognee | Apache 2.0 |
| Session memory | MemPalace | Redis simple cache | MIT |
| Vector search | pgvector (Postgres) | Qdrant | Apache 2.0 |
| Workflow automation | n8n | Apache Airflow | Apache 2.0 |
| LLM abstraction | LiteLLM | direct SDK | MIT |
| Observability | LangSmith + OTEL | Langfuse (OSS) | Closed + Apache 2.0 |
| KG bootstrap | Microsoft GraphRAG | LlamaIndex KG | MIT |
| Ontology authoring | Protégé (design-time) | — | LGPL |

---

## 7. WHAT GRILL QUESTIONS WILL DETERMINE

The following questions determine architecture fork points before planning:

1. **Domain order:** Which industry first for demo? (Telecom changes architecture significantly if TM Forum compliance needed)
2. **Channel priority:** Voice-first or chat-first? (Determines Pipecat vs. Chatwoot as day-1 focus)
3. **LLM hosting:** Cloud API (Claude/GPT-4) or self-hosted (Ollama/Llama)? (Determines data residency path)
4. **Infrastructure:** Fully self-hosted or cloud-managed? (Determines Neo4j AuraDB vs. self-hosted FalkorDB)
5. **Geography/language:** India-first (Hindi, regional languages) or English-first global?
6. **Multi-tenancy from day 1:** One client (demo) or multi-client SaaS architecture immediately?
7. **Escalation model:** Human agent UI needed from day 1 or can it be deferred?
8. **Compliance:** DPDP (India) requirements? GDPR? Air-gapped deployment needed?
9. **Timeline:** Demo in days/weeks or full production in months?
10. **Budget for APIs:** Monthly ceiling for Twilio/Deepgram/ElevenLabs/LLM costs?
11. **Business model:** B2B platform (sell to enterprises) or in-house tool?
12. **Existing integrations:** Do clients have Zendesk/Freshdesk/Jira already, or greenfield?
