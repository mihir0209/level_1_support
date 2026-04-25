# Agent Retrieval Gap Analysis — boohoo_uk

**Date:** 2026-04-23
**Brand evaluated:** boohoo_uk (330 Postgres chunks, 11 IssueType→Resolution paths, 20 FAQs, fully seeded graph)
**Test queries:** 12 across 8 intent categories

---

## Ground Truth (what boohoo_uk actually knows)

### In FalkorDB
| Data type | Count | Quality |
|---|---|---|
| Return Policy | 1 | ✅ 28 days, £1.99, 4 excluded categories |
| Shipping Policy (UK) | 1 | ✅ 2-5 days, £3.99, free over £30 |
| Shipping Policy (USA) | 1 | ✅ 6-8 days, £10.99, free over £100 |
| IssueType→Resolution | 11 | ✅ Steps populated (767-958 chars each) |
| Resolution.steps | All 11 | ✅ JSON steps with tool_required + conditions |
| FAQs (HAS_FAQ edges) | 20 | ✅ From ingestion pipeline |
| KnowledgeItems | 5 | ✅ Delivery/paperless/contact content |
| Graphiti history | — | ❌ No episodes yet (sessions haven't closed) |
| MemPalace | — | ❌ No sessions yet |

### In Postgres `knowledge_chunks`
| Data type | Count | Quality |
|---|---|---|
| FAQ chunks | ~200 | ✅ With metadata + keywords |
| Policy chunks | ~100 | ✅ From ingestion |
| KnowledgeItem chunks | ~30 | ✅ |
| **Embeddings** | **0 / 330** | ❌ **Completely missing** — vector column is NULL |

### What's NOT in the graph
- Shipping policy not included in `retrieve_knowledge()` — only return policy fetched
- Resolution descriptions (human-readable names only, no `description` field on Resolution nodes)
- `free_return_condition` on ReturnPolicy is `None` for all brands
- Most brands besides boohoo_uk have no graph data at all (no FAQs, no chunks)

---

## Evaluation Results

### Environment Comparison

| Query | Env 0 (none) | Env 1 (graph) | Env 2 (graph+KB) | Expected |
|---|---|---|---|---|
| wismo_01: Where is my order? | ✅ | ✅ | ✅ | Intent relevant |
| wismo_02: Late delivery | ✅ | ✅ | ✅ | Intent relevant |
| **return_01**: Return a dress | ~ | **✓** | **✓** | 28d, £1.99, exclusions |
| **return_02**: Return swimwear? | ❌ SWIMWEAR RETURNABLE | ❌ SWIMWEAR RETURNABLE | ❌ SWIMWEAR RETURNABLE | Should say CANNOT |
| **return_03**: How long to send back? | ~ | **✓** | **✓** | 28 days |
| **wrong_01**: Wrong colour item | ~ | ~ | ~ | Refund OR replacement |
| **refund_01**: 10 days since return | ~ | ~ | ~ | 14-day timeline |
| **refund_02**: Refund timeline? | ❌ 3-10 days | ❌ 3-7 days | ❌ 5-10 days | **14 days for cash** |
| account_01: Password reset | ✅ | ✅ | ✅ | Forgot password link |
| payment_01: Card failing | ✅ | ✅ | ✅ | Troubleshooting steps |
| **damage_01**: Tear in dress | ❌ Contact team photo | ❌ Replacement/refund | ❌ Replacement/refund | No return fee for damage |
| promo_01: Code not working | ~ | ~ | ~ | Validate steps |

**Summary scores:**
- Env 0: Avg 1.00 checks passed, 13 hallucination flags, 7/12 with policy detail
- Env 1: Avg 1.33 checks passed, 11 hallucination flags, 5/12 with policy detail
- Env 2: Avg 1.25 checks passed, 12 hallucination flags, 5/12 with policy detail

> The hallucination counts are inflated by a grading artifact: the grader checks for `"28"` in ALL answers regardless of query intent, so WISMO queries (which correctly don't mention return policy) get flagged. The 3 real hallucinations are: swimwear returns, wrong refund timeline, and missing no-return-fee for damaged items.

---

## Critical Gaps Found

### Gap 1: Refund Timeline Never Reaches the LLM [HIGH]

**Problem:** `get_return_policy()` returns `return_window_days`, `return_cost_gbp`, `excluded_categories`, but NOT `refund_timeline_days`. The `policy_block` always hardcodes "Up to 21 days (14 days transit + 7 days bank processing)" — this is wrong for Boohoo UK where the actual timeline is **14 days for cash refunds** (from the crawled FAQ content).

**Impact:** All refund timeline answers hallucinate. Every env says "3-7 days" or "5-10 days" — none say "14 days."

**Fix:** The ReturnPolicy node needs a `refund_timeline_days` property. The graph query needs to SELECT it. The `policy_block` needs to use it dynamically.

---

### Gap 2: Shipping Policy Not Retrieved for WISMO Queries [HIGH]

**Problem:** `retrieve_knowledge()` only calls `get_return_policy()`. Shipping policy exists in the graph (`get_shipping_policy()` function is already written!) but is never called.

**Impact:** For "Where is my order?" and late delivery queries, the LLM has no shipping timeline to quote. It gives generic "track with order number" answers without stating the expected delivery window.

**Fix:** Call `get_shipping_policy()` in `retrieve_knowledge()` for `wismo` and `delivery_query` intent types. Pass it as a `shipping_block` in context.

---

### Gap 3: Postgres Embeddings Are All NULL [HIGH]

**Problem:** 330 chunks exist in `knowledge_chunks` for boohoo_uk, but every `embedding` column is NULL. The ingestion pipeline calls `llm.embed()` but embeddings never get stored.

**Impact:** Vector search (Pattern C's semantic fallback) is completely non-functional. `search_kb_nodes()` can only do keyword matching. Semantic similarity search — the entire reason for the pgvector investment — is offline.

**Root cause:** Need to check whether `_embed_and_store_faqs()` is actually executing. Likely a silent failure in the embedding call.

**Fix:** Debug and fix the embedding pipeline. Add a migration to backfill embeddings for existing chunks.

---

### Gap 4: Swimsuit Return Policy — Answer Hallucinates [MEDIUM]

**Problem:** Every env answers "Yep, you can return swimwear" for "Can I return my swimwear?" The correct answer is "Unfortunately swimwear cannot be returned for hygiene reasons."

**Analysis:** The resolution steps DO say "swimwear, lingerie, cosmetics with broken seal" are excluded. The context block includes "Excluded items: swimwear, lingerie, cosmetics, and pierced jewellery." The LLM reads the exclusion list but still hallucinates.

**Fix:** This is an LLM instruction problem, not a retrieval problem. The context says swimwear is excluded, but the prompt tone ("You can totally return the dress") overrides the exclusion in the LLM's interpretation. Options:
1. Add an explicit instruction in the system prompt: "When excluded categories are listed, always lead with the exclusion, never imply items are returnable when they are not."
2. Pre-process the policy block to bold/highlight exclusions.

---

### Gap 5: Intent Classifier Misses Return Queries in Env 0 [MEDIUM]

**Problem:** In Env 0 (no knowledge graph), every single query gets classified as `unknown` intent. The intent classifier depends on brand-specific context to route correctly. Without the graph, it defaults to unknown.

**Impact:** The LLM in Env 0 gets the right answers anyway because it uses training knowledge, but it bypasses all the resolution step tooling and brand-specific policy context.

**Fix:** Ensure the intent classifier is robust enough to classify correctly without graph context. This is primarily a training/data issue.

---

### Gap 6: Resolution Steps Don't Include "No Return Fee for Damage" [MEDIUM]

**Problem:** For `damage_01` ("dress has a tear"), the answer says "reach out to customer service" or "choose replacement or refund." The resolution steps for `damaged_item_resolution` say: "No return fee charged for faulty items — brand covers return cost." This critical detail never appears in answers.

**Analysis:** The context block includes the steps, but the LLM summarises/generates its own answer and omits the no-return-fee detail. The resolution step is step 2 of 3 — the LLM stops after offering replacement/refund without reaching the cost detail.

**Fix:** The resolution block should highlight the most actionable/unique details. For damaged items, "no return fee" is the key differentiator from standard returns. Consider adding a `highlights` field to Resolution nodes or reordering step presentation.

---

### Gap 7: Most Brands Have Zero Graph Data [MEDIUM]

**Problem:** boohoo_uk is the only brand with populated FAQs, chunks, and policies. The other 12 brands (plt_uk, karenmillen_uk, etc.) have Brand nodes and IssueType links but no Policies, no FAQs, no KnowledgeItems.

**Impact:** Running this evaluation against plt_uk would show near-zero scores across all envs. The system is effectively single-brand-ready.

**Fix:** Run the ingestion pipeline for each brand. Seed ReturnPolicy and ShippingPolicy nodes for brands that haven't been crawled yet.

---

### Gap 8: "Brand Knowledge Base" Block Not Reaching LLM [LOW]

**Problem:** In Env 2, `retrieve_knowledge()` returns 0 `kb_items` for every query — `search_kb_nodes()` returns nothing. The 330 Postgres chunks and 20 graph FAQs are completely invisible to the retrieval layer.

**Root cause (search_kb_nodes):**
1. It searches FalkorDB, not Postgres
2. The 20 FAQs exist as `(:FAQ)` nodes connected to Brand via `[:HAS_FAQ]`, but `search_kb_nodes` looks for `FAQs` via `MATCH (b:Brand {id: $brand_id})-[]->(node) WHERE node:FAQ...` — it should find them
3. But the keyword extraction fails: "Where is my order?" → keywords = `["order"]` (stop word filtering leaves only "order"), and the FAQ content has "track", "tracking" not "order"

**Fix:** `search_kb_nodes` should search Postgres `knowledge_chunks` (which has 330 real crawled FAQs) via a vector similarity search. The FalkorDB KB search should be secondary. Currently it's the other way around.

---

## Priority Fix Plan

### Phase 1 — Immediate Quality Fixes (this session)

**1.1 Add refund_timeline_days to ReturnPolicy and graph query**
- `seed_data.py`: Add `refund_timeline_days: 14` to boohoo_uk's ReturnPolicy
- `retriever.py`: SELECT `p.refund_timeline_days` in `get_return_policy()` query
- `engine.py`: Update `_format_policy_block()` to use `policy.get("refund_timeline_days", 14)` dynamically instead of hardcoding 21 days

**1.2 Wire get_shipping_policy into retrieve_knowledge**
- `retriever.py`: Call `get_shipping_policy()` for `wismo`/`delivery_query` intent
- `engine.py`: Add `shipping_block` to context, inject into system prompt for delivery queries

**1.3 Fix grading artifact in evaluate_agent.py**
- Only check return_window accuracy for return/intent queries, not all queries
- This will give a true hallucination count

### Phase 2 — Data Completeness

**2.1 Fix Postgres embedding pipeline**
- Debug `_embed_and_store_faqs()` — why are embeddings NULL?
- Add `_backfill_embeddings()` script to embed all 330 existing chunks
- After fixing: vector similarity search becomes functional

**2.2 Fix search_kb_nodes to search Postgres first**
- Change `search_kb_nodes` to query Postgres `knowledge_chunks` via keyword similarity
- Keep FalkorDB as secondary for graph-structured data (resolutions, policies)

**2.3 Run ingestion for all 13 brands**
- `python -m ingestion.pipeline boohoo_uk https://boohoo.com` for each brand
- Target: at least 50 chunks per brand with embeddings

### Phase 3 — LLM Instruction Hardening

**3.1 System prompt rule for exclusions**
- Add to brand system prompts: "When the excluded items list is provided, always state the exclusion FIRST before any return instructions."

**3.2 Resolution step highlighting**
- Add `highlights` field to Resolution nodes (top 3 key facts)
- Prepend highlights to resolution block so LLM always includes them

**3.3 Intent classifier improvement**
- Add more training examples for ambiguous queries (e.g., "I want to return" vs "I want to exchange")
- Test classifier accuracy on 50+ real query variants

### Phase 4 — Multi-brand readiness

**4.1 Seed policies for all 13 brands**
- Each brand needs ReturnPolicy + ShippingPolicy in FalkorDB
- Can be seeded from brand registry data without crawling

**4.2 Evaluate each brand**
- Run `scripts/evaluate_agent.py <brand_id>` for all 13 brands
- Identify which brands have ingestion data vs. seed-only data
- Target: all brands score ≥ 2.0 avg checks passed in Env 2

---

## Score Targets (what "production-ready" looks like)

| Metric | Current | Target |
|---|---|---|
| Avg checks passed | 1.25 (Env 2) | ≥ 2.5 / 3 |
| Hallucination flags (real) | ~3 | 0 |
| Wrong refund timeline | All refund queries | 0 |
| Wrong exclusion policy | 1/1 swimwear query | 0 |
| Missing no-return-fee detail | damage_01 | 0 |
| Postgres embeddings populated | 0/330 | 330/330 |
| Multi-brand coverage | boohoo_uk only | All 13 brands |
| Shipping policy in context | No | Yes for WISMO queries |
