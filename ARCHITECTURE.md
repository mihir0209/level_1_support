# World-Class AI-Native L1 Support Architecture
## E-Commerce Edition — Reference: Boohoo Group
*Designed: 2026-04-16 | Status: Architecture v1.0*

---

## 0. PROBLEM STATEMENT (CONCRETE)

**Boohoo Group** operates 13 distinct fashion brands (Boohoo, BoohooMAN, PrettyLittleThing,
Nasty Gal, Karen Millen, Coast, Oasis, Warehouse, Wallis, Dorothy Perkins, Debenhams, MissPap,
Burton) — each with standalone websites, separate identities, and independent customer bases.

**L1 query distribution (observed from public complaint data):**
```
64%  Returns & Refunds         → fully automatable
18%  Order tracking (WISMO)    → fully automatable via API
 9%  Delivery delays           → automatable with status + policy
 5%  Payment issues            → partly automatable
 3%  Product / size queries    → automatable from catalog
 1%  Account issues            → automatable
─────
80%+ resolvable without human intervention
```

**Core challenge:** 13 brands × high volume × per-brand policy variation × per-user order state
= a system that must be simultaneously brand-aware, user-aware, knowledge-aware, and channel-aware
in every single interaction.

---

## 1. ARCHITECTURAL PHILOSOPHY

> "Context is the product. The LLM is just the renderer."

Four governing principles:

**1. Context Engineering over Prompt Engineering**
The difference between a bad and good L1 AI agent is not the model — it's what context the
model receives. We engineer context assembly as a first-class system component.

**2. Knowledge Graph as the Ground Truth Layer**
No hallucination tolerance in support. Every policy statement, resolution step, and product
fact must be traceable to a source node in the knowledge graph. The graph IS the KB.

**3. Temporal Awareness as a First-Class Feature**
Boohoo's return window changed. PLT's fee structure changed. Karen Millen's shipping zones
changed. The system must know not just "what is the policy" but "what was the policy when
this order was placed." Bi-temporal KG = mandatory.

**4. Multi-Brand Isolation with Shared Infrastructure**
One platform, 13 brands. Each brand runs in its own graph namespace, system prompt, and
crawl config. Infrastructure is shared; customer experience is completely siloed.

---

## 2. SYSTEM ARCHITECTURE — OVERVIEW

```
╔══════════════════════════════════════════════════════════════════════╗
║                      CHANNEL INGRESS LAYER                           ║
║  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌──────────┐  ┌─────────┐  ║
║  │  Chat   │  │  Email  │  │  Voice  │  │WhatsApp  │  │ Social  │  ║
║  │(Chatwoot│  │(Chatwoot│  │(Pipecat │  │  API     │  │(future) │  ║
║  │ widget) │  │ inbox)  │  │+Twilio) │  │          │  │         │  ║
║  └────┬────┘  └────┬────┘  └────┬────┘  └────┬─────┘  └────┬────┘  ║
╚═══════╪════════════╪════════════╪═════════════╪══════════════╪═══════╝
        │            │            │             │              │
        └────────────┴────────────┴─────────────┴──────────────┘
                                  │
                    ┌─────────────▼─────────────┐
                    │    BRAND ROUTER            │
                    │  (subdomain / brand_id     │
                    │   from channel metadata)   │
                    └─────────────┬─────────────┘
                                  │ brand_id injected into every request
                                  │
╔═════════════════════════════════▼════════════════════════════════════╗
║                   CONTEXT ENGINEERING LAYER                           ║
║                                                                        ║
║  ┌──────────────────────────────────────────────────────────────────┐  ║
║  │                    CONTEXT ASSEMBLY ENGINE                        │  ║
║  │                                                                   │  ║
║  │  1. Intent Classifier (fast model)                               │  ║
║  │  2. Entity Extractor  (order_id, product, error_type)            │  ║
║  │  3. User Context Fetcher  (real-time API)                        │  ║
║  │  4. Graph Knowledge Retriever (GraphRAG)                         │  ║
║  │  5. Policy Resolver (temporal-aware)                             │  ║
║  │  6. Session Compressor (sliding window)                          │  ║
║  │  7. Tool Manifest Builder (what actions are available)           │  ║
║  │  8. Context Ranker + Packer (fit into token budget)             │  ║
║  └──────────────────────────────────────────────────────────────────┘  ║
╚═════════════════════════════════╤════════════════════════════════════╝
                                  │  ContextPackage → LangGraph
                                  │
╔═════════════════════════════════▼════════════════════════════════════╗
║                    ORCHESTRATION LAYER                                ║
║                                                                        ║
║   ┌─────────────────────────────────────────────────────────────┐     ║
║   │              LangGraph State Machine                         │     ║
║   │                                                              │     ║
║   │  ┌──────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │     ║
║   │  │TRIAGE│→ │  ROUTE   │→ │  RESOLVE │→ │CLOSE/ESCALATE │  │     ║
║   │  │intent│  │to agent  │  │ (tools)  │  │               │  │     ║
║   │  └──────┘  └──────────┘  └──────────┘  └───────────────┘  │     ║
║   │                                                              │     ║
║   │  State: {brand_id, customer_id, intent, entities,          │     ║
║   │          context_package, conversation_history,             │     ║
║   │          tool_calls, confidence, turn_count}               │     ║
║   └──────────────────────┬──────────────────────────────────────┘     ║
║                           │  routes to specialist via Swarms           ║
║   ┌───────────────────────▼──────────────────────────────────────┐    ║
║   │           Swarms HierarchicalSwarm  (Specialist Agents)      │    ║
║   │                                                               │    ║
║   │  [WISMO Agent] [Returns Agent] [Refund Agent]                │    ║
║   │  [Payment Agent] [Product Agent] [Account Agent]             │    ║
║   │  [Promotions Agent] [Escalation Agent]                       │    ║
║   └───────────────────────────────────────────────────────────────┘   ║
╚═════════════════════════════════╤════════════════════════════════════╝
                                  │
        ┌─────────────────────────┼──────────────────────────────┐
        │                         │                               │
╔═══════▼═══════╗    ╔════════════▼═══════════╗    ╔═════════════▼═════╗
║  KNOWLEDGE    ║    ║   USER CONTEXT LAYER   ║    ║   TOOL / ACTION   ║
║  GRAPH LAYER  ║    ║                        ║    ║      LAYER        ║
║               ║    ║  Graphiti              ║    ║                   ║
║  FalkorDB /   ║    ║  (temporal per-user    ║    ║  Order Lookup API ║
║  Neo4j        ║    ║   knowledge graph)     ║    ║  Return Init API  ║
║               ║    ║                        ║    ║  Refund Status API║
║  GraphRAG     ║    ║  MemPalace             ║    ║  Account Reset    ║
║  pipeline     ║    ║  (session verbatim     ║    ║  Ticket Create    ║
║               ║    ║   memory, 96.6% R@5)  ║    ║  Email Send       ║
║  pgvector     ║    ║                        ║    ║  n8n Workflows    ║
║  (semantic    ║    ║  Redis (session TTL)   ║    ║                   ║
║   fallback)   ║    ║                        ║    ║  Chatwoot HITL    ║
╚═══════════════╝    ╚════════════════════════╝    ╚═══════════════════╝
        │
╔═══════▼═══════════════════════════════════════════════════════════════╗
║               KNOWLEDGE INGESTION PIPELINE                             ║
║                                                                         ║
║  [Brand Help Center] → [Firecrawl] → [Extraction LLM] →               ║
║  [SHACL Validator]  → [Confidence Score] → [Graph Upsert]             ║
║                                                                         ║
║  Nightly: batch crawl + policy diff detection                          ║
║  Real-time: brand announces shipping delay → Kafka → graph update      ║
╚═════════════════════════════════════════════════════════════════════════╝
        │
╔═══════▼═══════════════════════════════════════════════════════════════╗
║               FEEDBACK & LEARNING LOOP                                  ║
║                                                                         ║
║  Resolution outcome → CSAT → successRate update on Resolution node     ║
║  Failed intents → Unknown Intent Queue → human labels → new IssueType  ║
║  Policy changes detected → Policy Diff Alert → human review queue      ║
╚═════════════════════════════════════════════════════════════════════════╝
```

---

## 3. LAYER 1 — E-COMMERCE SUPPORT ONTOLOGY

The ontology is the **vocabulary** of the system. It defines what entities exist, what properties
they have, and what relationships connect them. Built with Protégé, implemented in Neo4j.

### 3.1 Core Entity Classes

```
EcommerceSupport Ontology v1.0
├── Organisation
│   ├── BrandGroup          (Boohoo Group plc)
│   └── Brand               (Boohoo, PLT, Nasty Gal, ...)
│       ├── brandId: String
│       ├── domain: URL
│       ├── helpCenterUrl: URL
│       ├── defaultLocale: Locale
│       ├── tier: Enum[budget, mid, premium]  
│       └── targetDemographic: String
│
├── ProductDomain
│   ├── ProductCategory     (Dresses, Tops, Footwear, ...)
│   ├── Product
│   │   ├── sku: String
│   │   ├── name: String
│   │   ├── brandId: String
│   │   ├── priceRange: Range
│   │   └── availableSizes: [String]
│   ├── ProductVariant      (size, colour)
│   └── SizeGuide           (UK/US/EU size mappings per category)
│
├── PolicyDomain            ← TEMPORAL (tracked by Graphiti)
│   ├── ReturnPolicy
│   │   ├── returnWindowDays: Int
│   │   ├── returnCostUSD: Decimal
│   │   ├── freeReturnCondition: String    (e.g., "PLT Royalty members")
│   │   ├── excludedCategories: [String]  (swimwear, lingerie, cosmetics)
│   │   └── effectiveDate: DateTime
│   ├── ShippingPolicy
│   │   ├── region: String
│   │   ├── standardDays: Int
│   │   ├── standardCost: Decimal
│   │   ├── expressAvailable: Boolean
│   │   ├── freeThreshold: Decimal
│   │   └── effectiveDate: DateTime
│   ├── PaymentPolicy       (accepted methods, BNPL providers)
│   └── PromotionPolicy     (discount code rules, stacking, exclusions)
│
├── IssueDomain             ← THE CORE SUPPORT KG
│   ├── IssueType
│   │   ├── id: String       (wismo, return_delay, wrong_item, ...)
│   │   ├── name: String
│   │   ├── description: String
│   │   ├── brandId: String  (null = applies to all brands)
│   │   ├── category: Enum   (ORDER, RETURN, PAYMENT, PRODUCT, ACCOUNT)
│   │   └── avgResolutionTime: Duration
│   ├── KnownIssue          (specific recurring problem with known fix)
│   │   ├── symptom: String
│   │   ├── trigger: String  (e.g., "Hermes carrier delay in UK Nov-Dec")
│   │   └── confidence: Float
│   └── Resolution
│       ├── id: String
│       ├── steps: [ResolutionStep]
│       ├── requiresToolCall: Boolean
│       ├── successRate: Float        (updated by feedback loop)
│       ├── avgSatisfactionScore: Float
│       └── lastValidatedAt: DateTime
│
├── CustomerDomain
│   ├── Customer
│   │   ├── id: String (brand-scoped)
│   │   ├── brandId: String
│   │   ├── tier: Enum[standard, loyaltyMember, vip]
│   │   └── locale: Locale
│   ├── Order
│   │   ├── id: String
│   │   ├── status: Enum[processing, dispatched, delivered, returned, cancelled]
│   │   ├── placedAt: DateTime          ← CRITICAL for temporal policy lookup
│   │   └── totalValue: Decimal
│   ├── OrderItem           (Product + quantity + price at time of purchase)
│   └── Shipment
│       ├── trackingId: String
│       ├── carrier: String
│       ├── estimatedDelivery: DateTime
│       └── currentStatus: String
│
└── SupportDomain
    ├── SupportSession      (one conversation)
    │   ├── id: String
    │   ├── channel: Enum[chat, email, voice, whatsapp]
    │   ├── brandId: String
    │   ├── startedAt: DateTime
    │   └── outcome: Enum[resolved, escalated, abandoned]
    └── Ticket              (human-reviewed item)
        ├── priority: Enum[P1, P2, P3]
        └── assignedAgent: String
```

### 3.2 Core Relationships (Neo4j Property Graph)

```cypher
// Brand structure
(:BrandGroup)-[:OWNS]->(:Brand)
(:Brand)-[:HAS_POLICY {effectiveDate, expiryDate}]->(:ReturnPolicy)
(:Brand)-[:HAS_POLICY {effectiveDate, expiryDate}]->(:ShippingPolicy)
(:Brand)-[:HAS_CATALOG]->(:ProductCategory)

// Policy supersession (temporal)
(:ReturnPolicy)-[:SUPERSEDES {at: DateTime}]->(:ReturnPolicy)

// Issue-Resolution graph (THE core support map)
(:IssueType)-[:RESOLVED_BY {priority: Int}]->(:Resolution)
(:Resolution)-[:HAS_STEP {order: Int}]->(:ResolutionStep)
(:ResolutionStep)-[:CALLS_TOOL]->(:Tool {name, endpoint, auth})
(:Resolution)-[:MAY_ESCALATE_TO]->(:EscalationPath {trigger, tier})
(:KnownIssue)-[:INSTANCE_OF]->(:IssueType)
(:KnownIssue)-[:FIXED_BY]->(:Resolution)

// Product-Issue links (device+error→fix, adapted for ecom)
(:ProductCategory)-[:HAS_KNOWN_ISSUE]->(:KnownIssue)
(:Product)-[:HAS_KNOWN_ISSUE]->(:KnownIssue)

// Customer graph (brand-isolated)
(:Customer {brandId})-[:HAS_ORDER]->(:Order)
(:Order)-[:CONTAINS]->(:OrderItem)-[:IS_VARIANT]->(:ProductVariant)
(:Order)-[:HAS_SHIPMENT]->(:Shipment)
(:Customer)-[:HAD_SESSION]->(:SupportSession)
(:SupportSession)-[:RESOLVED_WITH]->(:Resolution)
(:SupportSession)-[:OPENED_TICKET]->(:Ticket)
```

### 3.3 Boohoo-Specific Knowledge Graph Seed Data

```
Brand Nodes (13 brands):
  Boohoo         → boohoo.com          → tier: budget,  demographic: women 16-30
  BoohooMAN      → boohooman.com       → tier: budget,  demographic: men 16-30
  PrettyLittleThing → plt.com          → tier: budget,  demographic: women 16-24
  Nasty Gal      → nastygal.com        → tier: mid,     demographic: women 18-35
  Karen Millen   → karenmillen.com     → tier: premium, demographic: women 30-50
  Coast          → coast-stores.com    → tier: premium, demographic: women 25-45
  Dorothy Perkins→ dorothyperkins.com  → tier: mid,     demographic: women 35-55
  Wallis         → wallis.co.uk        → tier: mid,     demographic: women 35-60
  Oasis          → oasis-stores.com    → tier: mid,     demographic: women 25-40
  Warehouse      → warehouse.co.uk     → tier: mid,     demographic: women 20-35
  Debenhams      → debenhams.com       → tier: mid,     demographic: women/men 25-55
  MissPap        → misspap.co.uk       → tier: budget,  demographic: women 16-25
  Burton         → burton.co.uk        → tier: mid,     demographic: men 20-40

Policy Nodes (per brand, temporal):
  ReturnPolicy { brandId: "boohoo_us", returnWindowDays: 28, returnCostUSD: 6.00,
                 freeReturnCondition: null, excludedCategories: ["swimwear","lingerie","cosmetics"],
                 effectiveDate: "2024-01-01" }
  ShippingPolicy { brandId: "boohoo_us", region: "USA", standardDays: 8, standardCost: 10.99,
                   expressAvailable: true, expressCost: 14.99, freeThreshold: 100.00,
                   effectiveDate: "2024-01-01" }

IssueType Nodes (brand=null means universal):
  wismo             → "Where is my order?"
  return_initiate   → "How do I return an item?"
  refund_delay      → "Where is my refund?"
  wrong_item        → "I received the wrong item"
  damaged_item      → "Item arrived damaged/faulty"
  order_cancel      → "Can I cancel my order?"
  payment_failed    → "My payment was declined"
  promo_not_applied → "My discount code isn't working"
  size_query        → "What size should I order?"
  account_locked    → "I can't log in to my account"
  klarna_return     → "How do returns work with Klarna?"  ← BNPL-specific
  afterpay_return   → "How do returns work with Afterpay?"
```

---

## 4. LAYER 2 — CONTEXT ENGINEERING ENGINE

This is the most critical layer. It runs before EVERY LLM call and determines what the model
knows. Every millisecond and every token here is deliberate.

### 4.1 Context Assembly Pipeline

```
Input: raw_message + session + brand_id + channel
Output: ContextPackage (ranked, packed to token budget)

STEP 1 — Intent Classification           [~50ms, cheap model: Claude Haiku / Gemini Flash]
  Input:  raw_message + brand_id
  Output: IntentResult { issue_type, confidence, entities_raw }
  
  IntentResult examples:
    "where is my boohoo order #12345" → { issue_type: "wismo", entities: {order_id: "12345"} }
    "I want to return my PLT dress"   → { issue_type: "return_initiate", entities: {brand: "plt"} }
    "my klarna payment didn't work"   → { issue_type: "payment_failed", entities: {bnpl: "klarna"} }

STEP 2 — Entity Extraction              [parallel with Step 3 if entities found]
  Extract: order_id, product_sku, return_reason, payment_method, promo_code, error_message
  Normalize: "order 12345" → order_id: "BH-12345-US" (brand-prefixed canonical form)

STEP 3 — User Context Fetch             [real-time, async, ~100ms]
  Calls: Customer API, Order API, Return API
  Returns:
    customer { id, tier, locale, totalOrders, openTickets }
    recentOrders [ { id, status, placedAt, items, shipment } ]  ← last 3 orders
    openReturns  [ { id, status, initiatedAt, refundETA } ]
    
  IMPORTANT: orders include placedAt timestamp (needed for temporal policy lookup in Step 5)

STEP 4 — Graph Knowledge Retrieval      [GraphRAG, ~80ms]
  Pattern A (pre-built tools, 90% of cases):
    query_issue_resolutions(
      issue_type=intent.issue_type,
      brand_id=brand_id
    )
    → returns: top-3 Resolution nodes with steps, successRate, tool requirements
    
  Pattern B (vector fallback, open-ended questions):
    If confidence < 0.7 OR intent.issue_type == "unknown":
      vector_search(raw_message, brand_id=brand_id, k=5)
      → semantic chunks from help center content

STEP 5 — Policy Resolution              [temporal-aware, ~30ms]
  Given: brand_id + order.placedAt (from Step 3)
  Query: "What was the return policy for brand X when this order was placed?"
  
  Cypher:
    MATCH (b:Brand {id: $brand_id})-[r:HAS_POLICY]->(p:ReturnPolicy)
    WHERE r.effectiveDate <= $order_placed_at
    AND (r.expiryDate IS NULL OR r.expiryDate > $order_placed_at)
    RETURN p ORDER BY r.effectiveDate DESC LIMIT 1
    
  Returns: exactly the policy the customer's order is governed by.
  This prevents agents from quoting a new policy to a customer whose order was under an old one.

STEP 6 — Session Compression             [sliding window, <20ms]
  If session.turnCount <= 4:  include full transcript
  If session.turnCount > 4:   compress oldest turns via summarization model
  Always preserve: first user message, last agent confirmation, any tool_call results
  Target: keep session context under 1500 tokens

STEP 7 — Tool Manifest                   [static lookup, <5ms]
  Based on intent.issue_type, return available tools:
    wismo          → [order_lookup, shipment_track]
    return_initiate→ [return_init_api, policy_lookup, email_label]
    refund_delay   → [refund_status_api, payment_provider_lookup]
    order_cancel   → [cancel_order_api] (only if status == "processing")
    account_locked → [password_reset_api]

STEP 8 — Context Packing                 [critical, <10ms]
  Token budget: 12,000 total, reserve 4,000 for response
  Available: 8,000 tokens for context
  
  Priority stack (pack in order, stop when full):
    1. System prompt (brand-specific)          ~300 tokens   ALWAYS
    2. User context (account + recent orders)  ~400 tokens   ALWAYS
    3. Resolved entities                       ~100 tokens   ALWAYS
    4. Policy (temporally correct)             ~300 tokens   ALWAYS
    5. Resolution steps (top match)            ~600 tokens   ALWAYS
    6. Resolution steps (2nd + 3rd match)      ~800 tokens   IF SPACE
    7. Session transcript (compressed)         ~1500 tokens  ALWAYS
    8. Vector fallback chunks                  ~remaining    IF NEEDED
    9. Tool manifest                           ~200 tokens   ALWAYS
    
  Output: ContextPackage object (structured dict, not raw string)
```

### 4.2 Brand System Prompt Architecture

Each brand has a system prompt composed of:

```
[STATIC BASE]           ← shared across all brands (core instructions)
[BRAND IDENTITY]        ← injected at brand_id resolution
[DYNAMIC POLICY BLOCK]  ← fetched from graph (Step 5)
[USER TIER BLOCK]       ← adapted based on customer tier
[CHANNEL ADAPTATION]    ← voice vs. chat vs. email formatting rules
```

Example for PrettyLittleThing chat:
```
You are PLT's AI support assistant. You represent PrettyLittleThing — a bold,
fashion-forward brand for women 16-24. Keep responses upbeat, concise, and use
casual language appropriate to the brand voice.

RETURN POLICY (governing this customer's order placed 2024-11-15):
  - Return window: 28 days from delivery
  - Return cost: £1.99 deducted from refund
  - Free returns: PLT Royalty members only
  - Excluded: swimwear, lingerie, cosmetics with broken seals

CUSTOMER TIER: Standard (not Royalty member)
NOTE: This customer has 2 open refunds currently processing.

TOOLS AVAILABLE: [return_initiate, refund_status_lookup, email_label_send]

ESCALATION TRIGGER: Escalate if: refund > 21 days overdue, customer explicitly 
requests human, or 3 failed resolution attempts within this session.
```

---

## 5. LAYER 3 — ORCHESTRATION (LangGraph State Machine)

### 5.1 State Schema

```python
class L1SupportState(TypedDict):
    # Immutable session identifiers
    session_id: str
    brand_id: str
    customer_id: str
    channel: str  # chat | email | voice | whatsapp
    
    # Conversation
    messages: Annotated[list, add_messages]   # LangGraph message accumulator
    turn_count: int
    
    # Intent & entities (updated each turn)
    intent: IntentResult | None
    entities: dict                # {order_id, product_sku, promo_code, ...}
    confidence: float
    
    # Context (assembled each turn)
    context_package: ContextPackage | None
    user_context: UserContext | None
    applicable_policy: Policy | None
    
    # Resolution tracking
    resolution_attempts: int
    current_resolution: Resolution | None
    tool_results: list[ToolResult]
    
    # Outcome
    is_resolved: bool
    escalation_reason: str | None
    satisfaction_predicted: float | None
```

### 5.2 LangGraph Node Definitions

```
Nodes:
  assemble_context     → runs Context Engineering Pipeline (Section 4)
  classify_intent      → intent + entity extraction (fast model)
  route_to_specialist  → selects specialist agent via Swarms
  execute_tools        → calls external APIs (order, return, refund APIs)
  generate_response    → main LLM call with full ContextPackage
  validate_response    → confidence check, hallucination guard
  check_resolution     → did the agent's response likely resolve the issue?
  escalate             → create Chatwoot ticket, assign to human agent
  close_session        → log outcome, update Resolution.successRate in KG

Edges:
  START → assemble_context
  assemble_context → classify_intent
  classify_intent → route_to_specialist
  route_to_specialist → execute_tools  (if tool required)
  route_to_specialist → generate_response  (if no tool required)
  execute_tools → generate_response
  generate_response → validate_response
  validate_response → check_resolution
  
  check_resolution:
    confidence >= 0.8 AND turn_count < 8 → (await next user message) → assemble_context
    resolution_attempts >= 3             → escalate
    customer requested human             → escalate
    refund > 21 days                     → escalate
    is_resolved == True                  → close_session
  
  escalate → close_session
  close_session → END
```

### 5.3 Specialist Agents (Swarms HierarchicalSwarm)

```
SUPERVISOR AGENT
  Role: Routes to correct specialist, maintains conversation coherence
  Model: Claude Sonnet (reasoning quality matters for routing)
  
SPECIALIST ROSTER:
  ┌─────────────────────────────────────────────────────────────┐
  │ Agent Name    │ Handles                   │ Tools           │
  ├───────────────┼───────────────────────────┼─────────────────┤
  │ WISMO Agent   │ Order tracking, ETA,      │ order_lookup,   │
  │               │ dispatch status           │ shipment_track  │
  ├───────────────┼───────────────────────────┼─────────────────┤
  │ Returns Agent │ Return initiation,        │ return_init,    │
  │               │ returns policy, labels    │ label_generate  │
  ├───────────────┼───────────────────────────┼─────────────────┤
  │ Refund Agent  │ Refund status, timelines, │ refund_status,  │
  │               │ BNPL (Klarna/Afterpay)    │ payment_lookup  │
  ├───────────────┼───────────────────────────┼─────────────────┤
  │ Product Agent │ Size guides, availability,│ catalog_lookup, │
  │               │ product info              │ size_guide_api  │
  ├───────────────┼───────────────────────────┼─────────────────┤
  │ Payment Agent │ Failed payments, billing, │ payment_retry,  │
  │               │ BNPL issues               │ card_verify     │
  ├───────────────┼───────────────────────────┼─────────────────┤
  │ Promos Agent  │ Discount codes, vouchers, │ promo_validate, │
  │               │ loyalty points            │ loyalty_lookup  │
  ├───────────────┼───────────────────────────┼─────────────────┤
  │ Account Agent │ Login, password reset,    │ reset_password, │
  │               │ profile updates           │ verify_email    │
  ├───────────────┼───────────────────────────┼─────────────────┤
  │ Escalation   │ Human handoff, ticket     │ chatwoot_ticket,│
  │ Agent         │ creation, priority triage │ email_alert     │
  └─────────────────────────────────────────────────────────────┘
```

---

## 6. LAYER 4 — KNOWLEDGE GRAPH (Graph DB + GraphRAG)

### 6.1 Database: FalkorDB (Primary) + pgvector (Secondary)

**FalkorDB** chosen over Neo4j for this use case:
- Apache 2.0 license (fully open source, no AGPL concern)
- Redis-based → sub-millisecond read latency
- Cypher-compatible (same query language as Neo4j)
- GraphBLAS-powered (sparse linear algebra for traversal)
- Self-hosted Docker image: `falkordb/falkordb`

**pgvector** in same Postgres instance:
- Vector search for semantic fallback
- Eliminates a separate vector database
- Full-text search (tsvector) for BM25-style keyword matching

### 6.2 GraphRAG Retrieval Pipeline

```python
# Pre-built tool: used for 90% of predictable L1 queries (safe, fast)
def get_resolutions_for_issue(
    brand_id: str,
    issue_type: str,
    order_placed_at: datetime | None = None
) -> list[Resolution]:
    
    cypher = """
    MATCH (b:Brand {id: $brand_id})
    MATCH (it:IssueType {id: $issue_type})
    MATCH (it)-[r:RESOLVED_BY]->(res:Resolution)
    
    // Optionally fetch temporally-correct policy
    OPTIONAL MATCH (b)-[hp:HAS_POLICY]->(p:ReturnPolicy)
    WHERE hp.effectiveDate <= $order_placed_at
    AND (hp.expiryDate IS NULL OR hp.expiryDate > $order_placed_at)
    
    RETURN res, p
    ORDER BY res.successRate DESC, r.priority ASC
    LIMIT 3
    """
    return graph.query(cypher, {brand_id, issue_type, order_placed_at})

# Text2Cypher: fallback for unstructured/novel queries
def text2cypher_search(
    natural_language: str,
    brand_id: str,
    schema: GraphSchema
) -> list[GraphResult]:
    # LLM generates Cypher from schema + examples
    # Read-only FalkorDB user (injection mitigation)
    # Schema injected as few-shot examples in system prompt
    cypher = llm.generate_cypher(
        query=natural_language,
        schema=schema,
        few_shot_examples=CYPHER_EXAMPLES
    )
    return graph.readonly_query(cypher)

# Vector semantic fallback
def vector_fallback_search(
    query: str,
    brand_id: str,
    k: int = 5
) -> list[Chunk]:
    embedding = embed(query)
    return pgvector.query(
        embedding,
        where={"brand_id": brand_id},
        k=k
    )
```

### 6.3 Multi-Brand Isolation in Graph

```cypher
// All brand-specific queries MUST include brand_id filter
// Convention: brand_id as first WHERE clause, enforced by query builder

// Example: get PLT return policy
MATCH (b:Brand {id: "prettylittlething_uk"})
      -[:HAS_POLICY]->(p:ReturnPolicy)
WHERE p.effectiveDate <= datetime("2025-11-15")
RETURN p

// Query builder enforces this:
class BrandScopedQueryBuilder:
    def __init__(self, brand_id: str):
        self.brand_id = brand_id
    
    def build(self, intent: str) -> str:
        # Always prefixes with brand_id WHERE clause
        # Prevents cross-brand data leakage
```

---

## 7. LAYER 5 — USER CONTEXT (Temporal Memory)

### 7.1 Memory Architecture: Three Tiers

```
TIER 1: Working Memory (Redis, TTL = session duration)
  - Current session transcript
  - In-progress tool call results
  - Mid-resolution state
  - Expires when session ends

TIER 2: Session Memory (MemPalace, persists 30 days)
  - Verbatim storage of each interaction (no summarization — preserves exact words)
  - Organized: Wing=brand, Room=customer_id, Drawer=session
  - 96.6% R@5 recall without LLM calls
  - Used for: "customer asked about their Karen Millen return last week"
  
TIER 3: Long-Term Temporal KG (Graphiti in Neo4j/FalkorDB)
  - Facts with bi-temporal validity windows
  - "Customer's PLT return was initiated 2025-11-01, refund processed 2025-11-21"
  - "Customer's preferred size is UK 10 as of last 3 orders"
  - "Customer has contacted 3 times about the same order #PLT-789"
  - Queried at session start to personalize every new interaction
```

### 7.2 Session Start Ritual

```python
async def initialize_session(brand_id: str, customer_id: str) -> SessionContext:
    
    # Parallel fetch (all async, ~100ms total)
    user_data, recent_history, open_issues = await asyncio.gather(
        customer_api.get(customer_id, brand_id),
        graphiti.get_recent_facts(customer_id, days=30, brand_id=brand_id),
        mempalace.recall(f"customer:{customer_id} brand:{brand_id} unresolved", k=3)
    )
    
    # Personalization signals
    signals = {
        "contact_frequency": len(recent_history),      # 3 contacts in 30 days = frustrated customer
        "last_issue_type": recent_history[0].issue_type if recent_history else None,
        "open_returns": [o for o in user_data.orders if o.status == "return_pending"],
        "loyalty_tier": user_data.tier,
    }
    
    # Adapt agent tone based on signals
    if signals["contact_frequency"] >= 3:
        tone_override = "empathetic_frustrated"     # extra patience, avoid repeating
    elif signals["loyalty_tier"] == "vip":
        tone_override = "premium_service"
    else:
        tone_override = "standard"
    
    return SessionContext(user_data, signals, tone_override)
```

---

## 8. LAYER 6 — DATA INGESTION PIPELINE

### 8.1 The "Crawl-to-Graph" Pipeline

```
SOURCE: Boohoo Group brand help centers (13 URLs)
TRIGGER: Nightly scheduled (Airflow) + real-time Kafka (incident events)

STAGE 1 — DISCOVERY
  For each brand: fetch sitemap.xml
  Filter: /help/, /faq/, /returns/, /delivery/, /contact/
  Deduplicate: hash URL + last-modified header
  Output: URL queue per brand

STAGE 2 — CRAWL (Firecrawl MCP)
  Input: URL queue
  Output: Markdown content per page
  Handles: JS-rendered pages, cookie banners, pagination
  Rate limit: 10 req/sec per domain (polite crawl)
  Store: raw_content table in Postgres (source of truth)

STAGE 3 — EXTRACTION (LLM-based NER/RE)
  For each page:
    prompt = f"""
    Extract structured support knowledge from this help page.
    Brand: {brand_id}
    URL: {url}
    
    Extract:
    1. PolicyFacts: {{"type": ..., "value": ..., "unit": ..., "condition": ...}}
       e.g., {{"type": "return_window", "value": 28, "unit": "days"}}
    
    2. IssueSolutions: {{"issue_type": ..., "steps": [...], "conditions": [...]}}
       e.g., {{"issue_type": "wismo", "steps": ["Check email for tracking link", ...]}}
    
    3. ExcludedItems: categories not covered by standard policies
    
    Output strict JSON. If uncertain, set confidence < 0.7.
    """
    result = claude.extract(prompt, content)

STAGE 4 — VALIDATION (SHACL)
  Validate extracted JSON against ontology shape constraints:
    - returnWindowDays must be Int > 0
    - effectiveDate must be parseable DateTime
    - issue_type must exist in IssueType vocabulary
  Reject records that fail validation (log to review queue)

STAGE 5 — CONFIDENCE THRESHOLD
  Auto-upsert: confidence >= 0.85
  Review queue: 0.7 <= confidence < 0.85
  Reject: confidence < 0.7
  Human reviews queue daily (10-min task)

STAGE 6 — CHANGE DETECTION
  Compare new extraction against existing graph node
  If policy value changed:
    → Create new Policy node
    → Add SUPERSEDES edge from old to new
    → Set expiryDate on old node
    → Alert: "PLT return window changed from 28 to 21 days"
    → Trigger human review before publishing

STAGE 7 — GRAPH UPSERT
  MERGE (not CREATE) — idempotent
  Update embedding in pgvector
  Timestamp last_crawled_at on source node
```

### 8.2 Real-Time Incident Pipeline

```
Brand posts shipping delay notice → 
  Kafka event: {brand_id, incident_type: "carrier_delay", carrier: "Evri", region: "UK", eta: "2025-12-20"} →
  Consumer updates KnownIssue node in graph →
  Agent now automatically informs affected customers about the known delay
  (No human needed to update KB — graph is live)
```

---

## 9. LAYER 7 — VOICE CHANNEL (Pipecat)

### 9.1 Voice Pipeline Architecture

```
Inbound call → Twilio SIP trunk → Daily.co WebRTC room
                                          │
                                   Pipecat Pipeline
                                          │
              ┌───────────────────────────┼────────────────────────┐
              │           │               │              │          │
         [VAD]     [STT: Deepgram    [LangGraph    [TTS:          [DTMF
    (Voice Activity  Nova-2,         L1 Agent]     ElevenLabs/    handler]
     Detection)     ~200ms latency]               Cartesia]
              │           │               │              │
              └───────────┴───────────────┴──────────────┘
                                   │
                              Frame Stream
                              (AudioFrame → TextFrame → LLMFrame → AudioFrame)
```

### 9.2 Voice-Specific Adaptations

```python
# Context packing for voice: shorter responses, no markdown
VOICE_CONTEXT_ADDITIONS = """
VOICE FORMAT RULES:
- Maximum 2 sentences per response
- Never use bullet points or markdown
- Spell out numbers: "twenty-eight days" not "28 days"  
- Confirm understanding before moving to next step
- Use natural speech: "Got it, let me check that for you"
"""

# Brand voice adaptation for voice channel
VOICE_BRAND_PERSONAS = {
    "prettylittlething": "Friendly, upbeat, gen-Z casual tone",
    "karen_millen": "Warm, professional, premium tone",
    "boohoo": "Helpful, casual, efficient",
}

# Interruption handling: Pipecat's built-in
# If user starts speaking mid-response → stop TTS, re-process from interrupt point
```

---

## 10. LAYER 8 — FEEDBACK & LEARNING LOOP

### 10.1 Resolution Outcome Tracking

```python
async def close_session(state: L1SupportState, outcome: str, csat: float | None):
    
    # 1. Update Resolution node success rate (running average)
    if state.current_resolution and outcome == "resolved":
        graph.query("""
        MATCH (r:Resolution {id: $res_id})
        SET r.successRate = (r.successRate * r.sampleSize + 1.0) / (r.sampleSize + 1),
            r.sampleSize = r.sampleSize + 1,
            r.avgSatisfactionScore = (r.avgSatisfactionScore * r.sampleSize + $csat) / (r.sampleSize + 1)
        """, {res_id: state.current_resolution.id, csat: csat or 0.0})
    
    # 2. Log SupportSession to Graphiti (temporal memory)
    await graphiti.add_episode(
        content=f"Customer {state.customer_id} contacted about {state.intent.issue_type}. "
                f"Outcome: {outcome}. Brand: {state.brand_id}.",
        metadata={"customer_id": state.customer_id, "brand_id": state.brand_id,
                  "issue_type": state.intent.issue_type}
    )
    
    # 3. Unknown intent → label queue
    if state.intent.issue_type == "unknown":
        label_queue.push({
            "message": state.messages[0].content,
            "brand_id": state.brand_id,
            "suggested_label": llm.suggest_label(state.messages[0].content)
        })
    
    # 4. Failed resolution → review queue
    if outcome == "escalated" and state.resolution_attempts >= 2:
        review_queue.push({
            "issue_type": state.intent.issue_type,
            "resolution_tried": state.current_resolution,
            "conversation": state.messages,
            "failure_reason": state.escalation_reason
        })
```

### 10.2 Weekly Knowledge Refinement

```
Weekly automated job:
  1. Pull all failed resolutions from review_queue
  2. LLM analyzes patterns: "Return_delay resolution failing 40% when Klarna involved"
  3. Proposes: new Resolution node for "klarna_return_delay" specifically
  4. Human approves → new node added to graph
  5. WISMO agent now routes Klarna-specific returns to improved resolution
```

---

## 11. LAYER 9 — OBSERVABILITY

Every LLM call, graph query, tool call, and context assembly step is traced.

```
Stack:
  LangSmith    → full LangGraph trace (each node, inputs, outputs, latency)
  OpenTelemetry→ spans for graph queries, API calls, cache hits
  Langfuse     → LLM cost tracking, prompt version management (open-source alternative)
  Prometheus   → operational metrics (resolution rate, escalation rate, latency p99)
  Grafana      → dashboards (per brand, per intent type, per channel)

Key Metrics:
  resolution_rate         → % sessions resolved without human (target: >80%)
  escalation_rate         → % sessions escalated to human
  first_contact_resolution→ % resolved in single turn
  context_assembly_latency→ p99 ms for ContextPackage assembly
  graph_query_latency     → p99 ms for FalkorDB queries
  intent_classification   → confusion matrix per brand (weekly review)
  knowledge_staleness     → last_crawled_at per brand (alert if > 24h)
```

---

## 12. COMPLETE TECHNOLOGY STACK

```
╔═══════════════════════════════════════════════════════════════════╗
║  COMPONENT                    TECHNOLOGY          LICENSE         ║
╠═══════════════════════════════════════════════════════════════════╣
║  Chat frontend / inbox         Chatwoot            MIT            ║
║  Voice pipeline                Pipecat             BSD-2          ║
║  Telephony                     Twilio / Plivo      Commercial     ║
║  STT                           Deepgram Nova-2     Commercial     ║
║  TTS                           ElevenLabs/Cartesia Commercial     ║
║  Web crawler                   Firecrawl           Commercial     ║
║  Agent orchestration           LangGraph v1.0      MIT            ║
║  Multi-agent patterns          Swarms              Apache 2.0     ║
║  LLM (primary)                 Claude Sonnet 4.x   Commercial     ║
║  LLM (fast/cheap)              Claude Haiku / Gemini Flash        ║
║  LLM abstraction               LiteLLM             MIT            ║
║  Knowledge Graph               FalkorDB            Apache 2.0     ║
║  GraphRAG pipeline             neo4j-graphrag-py   Apache 2.0     ║
║  Temporal KG (user memory)     Graphiti            Apache 2.0     ║
║  Session memory                MemPalace           MIT            ║
║  Session cache                 Redis               BSD            ║
║  Vector search                 pgvector (Postgres) Apache 2.0     ║
║  KG bootstrap (one-time)       Microsoft GraphRAG  MIT            ║
║  Ontology authoring            Protégé (design)    LGPL           ║
║  Workflow automation           n8n                 Apache 2.0     ║
║  Data pipeline (batch)         Airflow             Apache 2.0     ║
║  Streaming (real-time)         Kafka               Apache 2.0     ║
║  LLM observability             LangSmith           Commercial     ║
║  Metrics / tracing             OpenTelemetry+Prom  Apache 2.0     ║
║  Dashboards                    Grafana             AGPL           ║
║  Deployment (dev)              Docker Compose      —              ║
║  Deployment (prod)             Kubernetes + Helm   Apache 2.0     ║
╚═══════════════════════════════════════════════════════════════════╝
```

---

## 13. MULTI-BRAND ONBOARDING PLAYBOOK

When a new brand (e.g., Debenhams) is onboarded:

```
Step 1 — Config (5 minutes)
  Add Brand node to graph:
    { id: "debenhams_uk", name: "Debenhams", domain: "debenhams.com",
      tier: "mid", helpCenterUrl: "debenhams.com/help" }

Step 2 — Crawl (automated, ~2 hours)
  Crawl triggers on new brand_id
  Pipeline runs automatically: crawl → extract → validate → upsert

Step 3 — Policy Review (30 minutes, human)
  Review extracted policies in admin UI
  Approve/correct return window, shipping costs, exclusions

Step 4 — System Prompt (10 minutes)
  Write brand_identity section for Debenhams
  Define tone: warm, trusted British department store

Step 5 — Test (30 minutes)
  Run 20 test queries covering top L1 intents
  Verify graph returns correct policy nodes
  Verify agent responses match expected outputs

Total onboarding: ~3 hours per brand (after initial platform is live)
```

---

## 14. BOOHOO GROUP — IMPLEMENTATION PRIORITY

Given 80%+ of L1 queries are Returns + WISMO:

```
Phase 1 (Week 1-2): Core Resolution Path
  ✓ FalkorDB graph with ontology schema
  ✓ Boohoo brand seed data (policies, IssueTypes, Resolutions)
  ✓ LangGraph state machine (4 nodes: assemble → classify → resolve → close)
  ✓ WISMO Agent + Returns Agent (2 specialists)
  ✓ Order API integration (tracking, status)
  ✓ Chatwoot chat widget (one brand: Boohoo UK)
  → Handles: 82% of queries

Phase 2 (Week 3-4): Full Specialist Suite
  ✓ All 8 specialist agents
  ✓ MemPalace session memory integration
  ✓ Graphiti long-term customer memory
  ✓ Context Assembly Engine (full pipeline)
  ✓ Multi-brand: add PLT, Nasty Gal
  → Handles: 96% of queries

Phase 3 (Week 5-6): Knowledge Pipeline + Voice
  ✓ Firecrawl ingestion pipeline (all 13 brands)
  ✓ Policy change detection + alert system
  ✓ Pipecat voice channel (Boohoo UK first)
  ✓ Temporal policy resolution (Graphiti)
  ✓ LangSmith observability

Phase 4 (Week 7-8): Production Hardening
  ✓ Kubernetes deployment
  ✓ All 13 brands onboarded
  ✓ Feedback loop + knowledge refinement
  ✓ Email channel (Chatwoot inbox)
  ✓ Performance tuning: p99 context assembly < 200ms
```

---

## 15. WHAT MAKES THIS WORLD-CLASS

| Dimension | What Most Systems Do | What This System Does |
|---|---|---|
| Knowledge | Flat RAG over PDFs | Property graph with typed relationships, traversal-based retrieval |
| Policy | Static prompt text | Bi-temporal graph query — correct policy for the exact order date |
| Memory | Single-turn stateless | 3-tier: session (Redis) + short-term (MemPalace) + temporal KG (Graphiti) |
| Multi-brand | Separate deployments | One platform, 13 brands, graph-namespaced, shared infra |
| Context | Dump everything in prompt | Ranked, packed, budget-aware ContextPackage with 8-step assembly |
| Routing | Single LLM answers all | HierarchicalSwarm routes to specialist agents with domain expertise |
| Learning | Static KB | ResolutionNode.successRate updates after every interaction |
| Policy updates | Manual KB edits | Automated crawl → extract → SHACL validate → graph upsert → diff alert |
| Temporal | Ignores when order placed | Cypher query fetches exact policy in effect at order.placedAt |
| Escalation | Random or time-based | Signal-based: contact frequency, failed attempts, overdue thresholds |
| Voice | Separate system | Same LangGraph state machine, channel adapter adjusts format |
| Observability | Logs | Full LangSmith trace + OTel spans + Prometheus metrics + Grafana dashboards |
