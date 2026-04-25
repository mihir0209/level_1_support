"""
Knowledge Graph retrieval layer — GraphRAG implementation.

Three patterns:
  A. Pre-built tool functions  — 90% of predictable L1 queries (safe, fast)
  B. Text2Cypher fallback      — novel/open-ended queries (read-only, schema-injected)
  C. KB node search            — collected FAQ/DeliveryOption/KnowledgeItem fallback

Multi-brand isolation: every query function enforces brand_id as first filter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from core.ontology.schema import get_graph_client

log = structlog.get_logger(__name__)

_STOP_WORDS = {
    "what", "when", "where", "how", "does", "can", "will", "is", "are", "do",
    "your", "the", "and", "or", "for", "my", "me", "you", "i", "a", "an",
    "about", "this", "that", "have", "has", "be", "with", "to", "of", "in",
    "it", "its", "not", "get", "got", "was", "were", "been", "from", "at",
    "by", "on", "up", "out", "if", "so", "but", "just",
}

# Map intent category → most-relevant collected node labels to search first
_CATEGORY_LABELS: dict[str, list[str]] = {
    "delivery":   ["DeliveryOption", "FAQ", "KnowledgeItem"],
    "return":     ["ReturnPolicy", "ReturnMethod", "ReturnEligibility", "FAQ", "Policy"],
    "product":    ["SizeGuide", "ProductCategory", "FAQ", "KnowledgeItem"],
    "order":      ["FAQ", "KnowledgeItem", "DeliveryOption"],
    "payment":    ["FAQ", "KnowledgeItem"],
    "account":    ["FAQ", "KnowledgeItem"],
    "promotions": ["FAQ", "KnowledgeItem"],
    "unknown":    ["FAQ", "KnowledgeItem", "DeliveryOption", "Policy"],
}


@dataclass
class RetrievalResult:
    resolutions: list[dict[str, Any]]
    policy: dict[str, Any] | None
    confidence: float
    source: str  # "graph_tool", "text2cypher", "kb_search"
    latency_ms: float = 0.0
    kb_items: list[dict[str, Any]] = field(default_factory=list)
    shipping_policy: dict[str, Any] | None = None


# ═══════════════════════════════════════════════════════════════════════════
# PATTERN A — PRE-BUILT TOOL FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def get_resolutions_for_issue(
    brand_id: str,
    issue_type_id: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Retrieve top-N resolution paths for a given issue type scoped to a brand.
    Brand_id=None on IssueType means universal (applies to all brands).
    """
    _, graph = get_graph_client()
    result = graph.query(
        """
        MATCH (it:IssueType {id: $issue_type_id})
        WHERE it.brand_id IS NULL OR it.brand_id = $brand_id
        MATCH (it)-[r:RESOLVED_BY]->(res:Resolution)
        RETURN res.id AS id,
               res.name AS name,
               res.steps AS steps,
               res.requires_tool_call AS requires_tool_call,
               res.success_rate AS success_rate,
               res.escalation_trigger AS escalation_trigger
        ORDER BY res.success_rate DESC, r.priority ASC
        LIMIT $limit
        """,
        {"issue_type_id": issue_type_id, "brand_id": brand_id, "limit": limit},
    )
    rows = result.result_set if result and result.result_set else []
    resolutions = []
    for row in rows:
        steps = row[2]
        if isinstance(steps, str):
            try:
                steps = json.loads(steps.replace("'", '"'))
            except Exception:
                steps = []
        resolutions.append({
            "id": row[0], "name": row[1], "steps": steps,
            "requires_tool_call": row[3],
            "success_rate": row[4] or 0.0,
            "escalation_trigger": row[5],
        })
    return resolutions


def get_return_policy(
    brand_id: str,
    order_placed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """
    Retrieve the temporally-correct return policy for a brand.
    If order_placed_at is given, fetch the policy that was active on that date.
    This is the core temporal-awareness feature of the system.
    """
    _, graph = get_graph_client()
    as_of = (order_placed_at or datetime.now(timezone.utc)).isoformat()
    result = graph.query(
        """
        MATCH (b:Brand {id: $brand_id})-[hp:HAS_POLICY {type: 'return'}]->(p:ReturnPolicy)
        WHERE hp.effectiveDate <= $as_of
        AND (p.expiry_date IS NULL OR p.expiry_date > $as_of)
        RETURN p.return_window_days AS window_days,
               p.return_cost_gbp AS cost_gbp,
               p.return_cost_usd AS cost_usd,
               p.free_return_condition AS free_condition,
               p.excluded_categories AS excluded,
               p.effective_date AS effective_date,
               p.refund_timeline_days AS refund_timeline_days
        ORDER BY hp.effectiveDate DESC
        LIMIT 1
        """,
        {"brand_id": brand_id, "as_of": as_of},
    )
    rows = result.result_set if result and result.result_set else []
    if not rows:
        return None
    row = rows[0]
    excluded = row[4]
    if isinstance(excluded, str):
        try:
            excluded = json.loads(excluded.replace("'", '"'))
        except Exception:
            excluded = []
    return {
        "return_window_days": row[0],
        "return_cost_gbp": row[1],
        "return_cost_usd": row[2],
        "free_return_condition": row[3],
        "excluded_categories": excluded or [],
        "effective_date": row[5],
        "refund_timeline_days": row[6] if len(row) > 6 else 14,
    }


def get_shipping_policy(
    brand_id: str,
    region: str = "UK",
    order_placed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Retrieve temporally-correct shipping policy for a brand+region."""
    _, graph = get_graph_client()
    as_of = (order_placed_at or datetime.now(timezone.utc)).isoformat()
    result = graph.query(
        """
        MATCH (b:Brand {id: $brand_id})-[hp:HAS_POLICY {type: 'shipping', region: $region}]->(p:ShippingPolicy)
        WHERE hp.effectiveDate <= $as_of
        AND (p.expiry_date IS NULL OR p.expiry_date > $as_of)
        RETURN p.standard_days_min, p.standard_days_max, p.standard_cost,
               p.express_available, p.express_days_max, p.express_cost,
               p.free_shipping_threshold
        ORDER BY hp.effectiveDate DESC
        LIMIT 1
        """,
        {"brand_id": brand_id, "region": region, "as_of": as_of},
    )
    rows = result.result_set if result and result.result_set else []
    if not rows:
        return None
    row = rows[0]
    return {
        "standard_days_min": row[0], "standard_days_max": row[1],
        "standard_cost": row[2], "express_available": row[3],
        "express_days_max": row[4], "express_cost": row[5],
        "free_shipping_threshold": row[6], "region": region,
    }


def get_brand_info(brand_id: str) -> dict[str, Any] | None:
    """Retrieve brand metadata — name, voice persona, tier."""
    _, graph = get_graph_client()
    result = graph.query(
        """
        MATCH (b:Brand {id: $brand_id})
        RETURN b.name, b.tier, b.voice_persona, b.target_demographic, b.default_locale
        """,
        {"brand_id": brand_id},
    )
    rows = result.result_set if result and result.result_set else []
    if not rows:
        return None
    row = rows[0]
    return {
        "id": brand_id, "name": row[0], "tier": row[1],
        "voice_persona": row[2], "target_demographic": row[3],
        "default_locale": row[4],
    }


def search_kb_nodes(
    brand_id: str,
    query_text: str,
    intent_category: str = "unknown",
    limit: int = 6,
) -> list[dict[str, Any]]:
    """
    Pattern C: search collected KB nodes (FAQ, KnowledgeItem, DeliveryOption, etc.)
    using keyword matching. Fires when no IssueType→Resolution path exists.
    Returns a list of {label, title, content} dicts ranked by keyword coverage.
    """
    words = re.findall(r'\b[a-z]{3,}\b', query_text.lower())
    keywords = [w for w in words if w not in _STOP_WORDS][:5]

    if not keywords:
        # Derive fallback keywords from category
        category_defaults = {
            "delivery": ["delivery", "shipping", "dispatch"],
            "return": ["return", "refund", "exchange"],
            "product": ["size", "product", "available"],
            "payment": ["payment", "pay", "price"],
        }
        keywords = category_defaults.get(intent_category, ["help", "information"])

    target_labels = _CATEGORY_LABELS.get(intent_category, _CATEGORY_LABELS["unknown"])
    labels_filter = " OR ".join(f"node:{lbl}" for lbl in target_labels)

    # Parameterised CONTAINS — safe against injection
    kw_param_names = [f"kw{i}" for i in range(len(keywords))]
    kw_conditions = " OR ".join(
        f"(toLower(coalesce(node.content, '') + ' ' + coalesce(node.title, '') + ' ' + coalesce(node.question, '') + ' ' + coalesce(node.answer, '')) CONTAINS ${kp})"
        for kp in kw_param_names
    )

    params: dict[str, Any] = {"brand_id": brand_id, "limit": limit}
    for i, kw in enumerate(keywords):
        params[f"kw{i}"] = kw

    try:
        _, graph = get_graph_client()
        result = graph.query(
            f"""
            MATCH (b:Brand {{id: $brand_id}})-[]->(node)
            WHERE ({labels_filter})
            AND ({kw_conditions})
            RETURN labels(node)[0] AS label,
                   coalesce(node.title, node.question, '') AS title,
                   coalesce(node.content, node.answer, '') AS content
            LIMIT $limit
            """,
            params,
        )
        rows = result.result_set if result and result.result_set else []
        items = [
            {"label": row[0], "title": row[1] or "", "content": row[2] or ""}
            for row in rows
            if row[2]  # skip empty content
        ]
        log.info(
            "kb_search_hit",
            brand_id=brand_id,
            intent_category=intent_category,
            keywords=keywords,
            hits=len(items),
        )
        return items
    except Exception as e:
        log.warning("kb_search_failed", brand_id=brand_id, error=str(e))
        return []


def update_resolution_success_rate(
    resolution_id: str,
    resolved: bool,
    satisfaction: float = 0.0,
) -> None:
    """Feedback loop: update Resolution node after each session outcome."""
    _, graph = get_graph_client()
    weight = 1.0 if resolved else 0.0
    graph.query(
        """
        MATCH (r:Resolution {id: $res_id})
        SET r.success_rate = CASE
            WHEN r.sample_size = 0 THEN $weight
            ELSE (r.success_rate * r.sample_size + $weight) / (r.sample_size + 1)
            END,
            r.sample_size = coalesce(r.sample_size, 0) + 1,
            r.avg_satisfaction_score = CASE
            WHEN r.sample_size = 0 THEN $satisfaction
            ELSE (r.avg_satisfaction_score * r.sample_size + $satisfaction) / (r.sample_size + 1)
            END
        """,
        {"res_id": resolution_id, "weight": weight, "satisfaction": satisfaction},
    )


# ═══════════════════════════════════════════════════════════════════════════
# PATTERN B — TEXT2CYPHER (novel queries)
# ═══════════════════════════════════════════════════════════════════════════

# Few-shot examples that teach the LLM valid Cypher patterns for this graph.
CYPHER_FEW_SHOT_EXAMPLES = """
Example 1: "What is the return window for PLT?"
MATCH (b:Brand {id: 'plt_uk'})-[:HAS_POLICY {type: 'return'}]->(p:ReturnPolicy)
WHERE p.expiry_date IS NULL
RETURN p.return_window_days

Example 2: "What shipping options does Karen Millen offer to UK customers?"
MATCH (b:Brand {id: 'karenmillen_uk'})-[:HAS_POLICY {type: 'shipping', region: 'UK'}]->(p:ShippingPolicy)
WHERE p.expiry_date IS NULL
RETURN p.standard_days_min, p.standard_days_max, p.standard_cost,
       p.express_available, p.express_cost, p.free_shipping_threshold

Example 3: "What are the resolution steps for a wrong item complaint?"
MATCH (it:IssueType {id: 'wrong_item'})-[:RESOLVED_BY]->(r:Resolution)
RETURN r.name, r.steps
"""

GRAPH_SCHEMA_SUMMARY = """
Nodes: Brand, ReturnPolicy, ShippingPolicy, IssueType, Resolution,
       FAQ, KnowledgeItem, DeliveryOption, SizeGuide, Policy, ProductCategory,
       ReturnMethod, ReturnEligibility, ContactMethod, Review
Key relationships:
  (Brand)-[:HAS_POLICY {type, effectiveDate, region?}]->(ReturnPolicy|ShippingPolicy)
  (IssueType)-[:RESOLVED_BY {priority}]->(Resolution)
  (Brand)-[:HAS_FAQ]->(FAQ)            FAQ has: question, answer, category
  (Brand)-[:HAS_KNOWLEDGE]->(KnowledgeItem|DeliveryOption|SizeGuide|ContactMethod)
  (Brand)-[:HAS_POLICY]->(Policy)      Policy has: policy_type, title, content
  (Brand)-[:HAS_REVIEW]->(Review)
  (Brand)-[:SELLS]->(ProductCategory)
Brand IDs follow pattern: {name}_{locale} e.g. boohoo_uk, plt_uk, nastygal_us, coast_uk
Always filter by brand_id. Read-only queries only (MATCH/RETURN/WHERE/ORDER BY/LIMIT).
"""


async def text2cypher_search(
    natural_language_query: str,
    brand_id: str,
    llm_client,
) -> list[dict[str, Any]]:
    """
    Fallback: LLM generates a Cypher query from natural language.
    Always executed on a READ-ONLY connection.
    """
    prompt = f"""You are a Cypher query generator for a customer support knowledge graph.

Graph schema:
{GRAPH_SCHEMA_SUMMARY}

Examples:
{CYPHER_FEW_SHOT_EXAMPLES}

Brand context: {brand_id}
User question: {natural_language_query}

Generate a valid, READ-ONLY Cypher query (MATCH/RETURN only, no CREATE/SET/DELETE).
Always include brand_id filter. Return ONLY the Cypher query, no explanation."""

    cypher = await llm_client.generate(prompt)

    # Safety guard: reject write operations
    forbidden = ["CREATE", "SET", "DELETE", "MERGE", "REMOVE", "DROP"]
    if any(kw in cypher.upper() for kw in forbidden):
        log.warning("text2cypher_blocked_write_attempt", cypher=cypher, brand_id=brand_id)
        return []

    try:
        _, graph = get_graph_client()
        result = graph.query(cypher)
        rows = result.result_set if result and result.result_set else []
        return [{"row": list(row)} for row in rows]
    except Exception as e:
        log.error("text2cypher_execution_failed", error=str(e), cypher=cypher)
        return []


# ═══════════════════════════════════════════════════════════════════════════
# UNIFIED RETRIEVE FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

def retrieve_knowledge(
    brand_id: str,
    issue_type_id: str,
    order_placed_at: datetime | None = None,
    region: str = "UK",
    query_text: str = "",
    intent_category: str = "unknown",
    llm_client=None,
) -> RetrievalResult:
    """
    Main entry point for the Context Assembly Engine (Step 4 + Step 5).

    Pattern A: pre-built IssueType→Resolution paths (fast, high-confidence).
    Pattern B (Text2Cypher): LLM generates a Cypher query for novel/unmatched queries.
    Pattern C: keyword search across collected KB nodes as last resort.

    Also retrieves shipping policy for WISMO/delivery intent types.
    """
    import time
    t0 = time.monotonic()

    resolutions = get_resolutions_for_issue(brand_id, issue_type_id)
    policy = get_return_policy(brand_id, order_placed_at)

    # Shipping policy is needed for WISMO and delivery queries
    shipping_policy: dict[str, Any] | None = None
    if issue_type_id in ("wismo", "delivery_query"):
        shipping_policy = get_shipping_policy(brand_id, region, order_placed_at)

    kb_items: list[dict] = []

    if not resolutions and query_text:
        # Try Text2Cypher first (Pattern B) — uses LLM to generate a targeted graph query
        if llm_client is not None:
            import asyncio
            kb_items = asyncio.get_event_loop().run_until_complete(
                text2cypher_search(query_text, brand_id, llm_client)
            )
            if kb_items:
                log.info("text2cypher_hit", brand_id=brand_id, items=len(kb_items))

        # Fallback: keyword search across KB nodes (Pattern C)
        if not kb_items:
            kb_items = search_kb_nodes(brand_id, query_text, intent_category)

    latency_ms = (time.monotonic() - t0) * 1000
    confidence = 0.95 if resolutions else (0.75 if kb_items else 0.3)
    source = "graph_tool" if resolutions else ("text2cypher" if kb_items else "kb_search")

    log.info(
        "knowledge_retrieved",
        brand_id=brand_id,
        issue_type=issue_type_id,
        resolution_count=len(resolutions),
        kb_items=len(kb_items),
        has_shipping=shipping_policy is not None,
        latency_ms=round(latency_ms, 1),
    )
    return RetrievalResult(
        resolutions=resolutions,
        policy=policy,
        confidence=confidence,
        source=source,
        latency_ms=latency_ms,
        kb_items=kb_items,
        shipping_policy=shipping_policy,
    )
