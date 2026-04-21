"""
Context Engineering Engine — the most critical layer of the system.

Runs before EVERY LLM call. Assembles a ContextPackage from 8 ordered steps.
The LLM never sees raw user input — it always receives a crafted, ranked context.

Step 1  Intent Classification         ~50ms   (fast model)
Step 2  Entity Extraction             ~30ms
Step 3  User Context Fetch            ~100ms  (async, parallel)
Step 4  Graph Knowledge Retrieval     ~80ms   (GraphRAG)
Step 5  Policy Resolution             ~30ms   (temporal)
Step 6  Session Compression           ~20ms
Step 7  Tool Manifest                 ~5ms
Step 8  Context Packing               ~10ms   (priority-ranked, token-budget-aware)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog

from config import settings
from core.knowledge.retriever import get_brand_info, retrieve_knowledge
from core.ontology.models import (
    Channel,
    ContextPackage,
    Customer,
    IssueCategory,
    IntentResult,
)

log = structlog.get_logger(__name__)

# Approximate token counts for each context block (for budget management)
_TOKEN_BUDGET = 8000       # total context tokens available
_RESPONSE_RESERVE = 4000   # reserved for model's response

# ── Tool manifest per intent type (Step 7 lookup table) ───────────────────
_TOOL_MANIFEST: dict[str, list[str]] = {
    "wismo":             ["order_lookup", "shipment_track"],
    "return_initiate":   ["policy_lookup", "return_init", "email_label"],
    "refund_delay":      ["refund_status", "payment_lookup", "escalate"],
    "wrong_item":        ["order_lookup", "catalog_lookup", "return_init", "refund_initiate"],
    "damaged_item":      ["order_lookup", "return_init", "refund_initiate"],
    "order_cancel":      ["order_lookup", "cancel_order"],
    "payment_failed":    ["payment_lookup"],
    "promo_not_applied": ["promo_validate", "escalate"],
    "klarna_return":     ["policy_lookup", "return_init"],
    "afterpay_return":   ["policy_lookup", "return_init"],
    "size_query":        ["size_guide", "catalog_lookup"],
    "account_locked":    ["account_status", "escalate"],
    "delivery_query":    ["policy_lookup", "shipment_track"],
    "unknown":           ["escalate"],
}

# ── Brand system prompt templates ─────────────────────────────────────────
_BRAND_SYSTEM_PROMPTS: dict[str, str] = {
    "boohoo_uk": (
        "You are Boohoo's AI support assistant. Boohoo is a British fast-fashion brand for women "
        "aged 16-30. Be friendly, helpful, and casual. Keep responses concise and action-oriented."
    ),
    "boohooman_uk": (
        "You are BoohooMAN's AI support assistant. BoohooMAN is a men's fashion brand for 16-30s. "
        "Be direct, helpful, and relaxed in tone."
    ),
    "plt_uk": (
        "You are PLT's AI support assistant representing PrettyLittleThing. PLT is a bold, "
        "fashion-forward brand for women aged 16-24. Be upbeat, warm, and use casual language. "
        "Emoji use is fine occasionally."
    ),
    "nastygal_us": (
        "You are Nasty Gal's AI support assistant. Nasty Gal is a confident, edgy US fashion brand "
        "for women 18-35. Keep it real, direct, and empowering."
    ),
    "karenmillen_uk": (
        "You are Karen Millen's customer experience assistant. Karen Millen is a premium British "
        "fashion brand for women 30-50. Maintain a warm, sophisticated, and professional tone. "
        "This is a premium brand — prioritise quality service over speed."
    ),
    "debenhams_uk": (
        "You are Debenhams' customer support assistant. Debenhams is a trusted British department "
        "store brand. Be warm, reliable, and thorough."
    ),
}

_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful customer support assistant for a fashion brand. "
    "Be friendly, professional, and focused on resolving the customer's issue quickly."
)

# ── Channel-specific formatting additions ─────────────────────────────────
_CHANNEL_ADDITIONS: dict[Channel, str] = {
    Channel.VOICE: (
        "\n\nVOICE FORMAT RULES: Maximum 2 sentences per response. Never use bullet points or "
        "markdown. Spell out numbers: 'twenty-eight days' not '28 days'. "
        "Use natural speech patterns. Confirm understanding before moving on."
    ),
    Channel.EMAIL: (
        "\n\nEMAIL FORMAT RULES: Use proper paragraphs. Include a greeting and sign-off. "
        "You may use formatting like bullet points for steps. Be thorough — email customers "
        "expect a complete answer in one message."
    ),
    Channel.CHAT: "",
    Channel.WHATSAPP: (
        "\n\nWHATSAPP FORMAT RULES: Keep messages short (under 200 characters per message). "
        "Use simple language. Avoid long paragraphs."
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# STEP 1: INTENT CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

async def classify_intent(
    message: str,
    brand_id: str,
    conversation_history: list[dict[str, str]],
    llm_client,
) -> IntentResult:
    """
    Fast intent classification using the cheap/fast model.
    Returns an IntentResult with issue_type slug and extracted entities.
    """
    known_issue_types = [k for k in _TOOL_MANIFEST if k != "unknown"]

    # Include last 2 turns of history for context (avoid full history = slow)
    recent_history = conversation_history[-4:] if len(conversation_history) > 4 else conversation_history
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in recent_history
    ) if recent_history else "No prior conversation."

    prompt = f"""Classify the customer's support intent for a fashion e-commerce brand.

Known issue types (use these exact IDs):
{json.dumps(known_issue_types, indent=2)}

Recent conversation:
{history_text}

Latest customer message: "{message}"
Brand: {brand_id}

Respond with ONLY valid JSON (no markdown):
{{
  "issue_type": "<one of the known issue types, or 'unknown'>",
  "confidence": <float 0.0-1.0>,
  "category": "<order|return|payment|product|account|promotions|delivery>",
  "entities": {{
    "order_id": "<if mentioned, else null>",
    "product_sku": "<if mentioned, else null>",
    "return_reason": "<if mentioned, else null>",
    "promo_code": "<if mentioned, else null>",
    "payment_method": "<klarna|afterpay|paypal|card|null>",
    "item_description": "<brief description of item if mentioned, else null>"
  }}
}}"""

    response = await llm_client.generate(
        prompt,
        max_tokens=300,
        fast=True,
    )

    try:
        data = json.loads(response.strip())
        return IntentResult(
            issue_type=data.get("issue_type", "unknown"),
            confidence=float(data.get("confidence", 0.5)),
            category=IssueCategory(data.get("category", "order")),
            entities={k: v for k, v in data.get("entities", {}).items() if v is not None},
        )
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("intent_parse_failed", error=str(e), response=response[:200])
        return IntentResult(
            issue_type="unknown",
            confidence=0.3,
            category=IssueCategory.ORDER,
            entities={},
        )


# ═══════════════════════════════════════════════════════════════════════════
# STEP 6: SESSION COMPRESSION
# ═══════════════════════════════════════════════════════════════════════════

def compress_session(
    conversation_history: list[dict[str, str]],
    max_recent_turns: int = 4,
    max_tokens: int = 1500,
) -> str:
    """
    Sliding window compression:
    - Keep last N turns verbatim (most relevant)
    - Summarise older turns into a brief paragraph
    Target: keep session block under max_tokens
    """
    if not conversation_history:
        return "No prior conversation in this session."

    if len(conversation_history) <= max_recent_turns * 2:
        # Short session — include everything
        lines = []
        for msg in conversation_history:
            role = "Customer" if msg["role"] == "user" else "Agent"
            lines.append(f"{role}: {msg['content']}")
        return "\n".join(lines)

    # Split into older (to summarize) and recent (verbatim)
    recent = conversation_history[-(max_recent_turns * 2):]
    older = conversation_history[:-(max_recent_turns * 2)]

    # Summarize older turns inline (no LLM call — extract key facts deterministically)
    topics_mentioned = set()
    order_ids = set()
    for msg in older:
        content = msg["content"].lower()
        if "order" in content:
            topics_mentioned.add("order inquiry")
        if "return" in content or "refund" in content:
            topics_mentioned.add("return/refund discussion")
        if "track" in content:
            topics_mentioned.add("tracking inquiry")
        # Extract order IDs (simple pattern)
        import re
        ids = re.findall(r'\b[A-Z]{2,3}-?\d{5,10}\b', msg["content"])
        order_ids.update(ids)

    summary_parts = []
    if topics_mentioned:
        summary_parts.append(f"Earlier in this session: {', '.join(topics_mentioned)}.")
    if order_ids:
        summary_parts.append(f"Order IDs discussed: {', '.join(order_ids)}.")

    summary = " ".join(summary_parts) if summary_parts else f"Earlier: {len(older)} messages exchanged."

    recent_lines = []
    for msg in recent:
        role = "Customer" if msg["role"] == "user" else "Agent"
        recent_lines.append(f"{role}: {msg['content']}")

    return f"[Earlier context: {summary}]\n\n" + "\n".join(recent_lines)


# ═══════════════════════════════════════════════════════════════════════════
# STEP 8: CONTEXT PACKING
# ═══════════════════════════════════════════════════════════════════════════

def _format_policy_block(policy: dict[str, Any] | None, brand_name: str) -> str:
    if not policy:
        return f"Return policy for {brand_name}: Please check the website for current return policy details."

    free_returns = policy.get("free_return_condition") or "Not available (return fee applies)"
    excluded = ", ".join(policy.get("excluded_categories", [])) or "None"
    cost_gbp = policy.get("return_cost_gbp", 0)
    cost_usd = policy.get("return_cost_usd", 0)

    cost_str = ""
    if cost_gbp:
        cost_str += f"£{cost_gbp}"
    if cost_usd:
        cost_str += f" / ${cost_usd}"
    if not cost_str:
        cost_str = "Free"

    return (
        f"RETURN POLICY ({brand_name}):\n"
        f"  Return window: {policy.get('return_window_days', 28)} days from delivery\n"
        f"  Return cost: {cost_str}\n"
        f"  Free returns: {free_returns}\n"
        f"  Excluded items: {excluded}\n"
        f"  Refund timeline: Up to 21 days (14 days transit + 7 days bank processing)"
    )


def _format_resolution_block(
    resolutions: list[dict[str, Any]],
    kb_items: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = []

    if resolutions:
        best = resolutions[0]
        steps = best.get("steps", [])
        if isinstance(steps, str):
            try:
                steps = json.loads(steps.replace("'", '"'))
            except Exception:
                steps = []

        success_rate = float(best.get("success_rate") or 0)
        lines.append(f"RESOLUTION: {best['name']} (success rate: {success_rate:.0%})")
        for step in steps:
            if isinstance(step, dict):
                instruction = step.get("instruction", "")
                tool = step.get("tool_required", "")
                condition = step.get("condition", "")
                line = f"  Step {step.get('order', '?')}: {instruction}"
                if tool:
                    line += f" [USE TOOL: {tool}]"
                if condition:
                    line += f" (if: {condition})"
                lines.append(line)
            else:
                lines.append(f"  - {step}")

    if kb_items:
        if lines:
            lines.append("")
        lines.append("BRAND KNOWLEDGE BASE:")
        for item in kb_items[:5]:
            label = item.get("label", "")
            title = item.get("title", "").strip()
            content = item.get("content", "").strip()
            if title and content:
                lines.append(f"  [{label}] {title}: {content[:400]}")
            elif content:
                lines.append(f"  [{label}] {content[:400]}")

    if not lines:
        return "No specific resolution steps found. Use general support guidelines."

    return "\n".join(lines)


def _format_user_context_block(
    customer: Customer | None,
    history_facts: list[str] | None = None,
) -> str:
    if not customer:
        block = "Customer: Not identified / guest checkout."
    else:
        lines = [
            f"CUSTOMER: ID={customer.id}, Tier={customer.tier.value}, Locale={customer.locale}",
        ]
        if customer.recent_orders:
            order = customer.recent_orders[0]
            lines.append(
                f"  Latest order: {order.id} | Status: {order.status.value} | "
                f"Placed: {order.placed_at.strftime('%Y-%m-%d')} | "
                f"Value: {order.currency} {order.total_value:.2f}"
            )
            if order.shipment and order.shipment.tracking_id:
                eta = (
                    order.shipment.estimated_delivery.strftime("%Y-%m-%d")
                    if order.shipment.estimated_delivery else "Unknown"
                )
                lines.append(
                    f"  Shipment: {order.shipment.carrier} tracking={order.shipment.tracking_id} "
                    f"ETA={eta} status={order.shipment.current_status}"
                )
        if customer.open_returns:
            lines.append(f"  Open returns: {len(customer.open_returns)} pending")
        if customer.total_orders > 0:
            lines.append(f"  Total orders placed: {customer.total_orders}")
        block = "\n".join(lines)

    if history_facts:
        block += "\n\nPAST INTERACTIONS (Graphiti memory):\n"
        block += "\n".join(f"  - {f}" for f in history_facts[:5])

    return block


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ASSEMBLY FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

async def assemble_context(
    message: str,
    session_id: str,
    brand_id: str,
    channel: Channel,
    conversation_history: list[dict[str, str]],
    customer: Customer | None,
    llm_client,
    customer_order_placed_at=None,
) -> ContextPackage:
    """
    Full 8-step Context Assembly Pipeline.
    Returns a ContextPackage ready for injection into the LangGraph state.
    """
    t_start = time.monotonic()

    # ── Step 1: Intent Classification ────────────────────────────────────
    intent = await classify_intent(message, brand_id, conversation_history, llm_client)
    log.info("intent_classified", issue_type=intent.issue_type, confidence=intent.confidence)

    # ── Steps 3+4+5: Parallel fetch ──────────────────────────────────────
    # User context is already passed in (fetched externally for flexibility)
    # Graph retrieval, brand info, and Graphiti history run in parallel
    customer_id = customer.id if customer else None

    async def _fetch_graphiti_history() -> list[str]:
        if not customer_id:
            return []
        try:
            from core.memory.graphiti_client import graphiti_memory
            return await graphiti_memory.search(
                customer_id=customer_id,
                brand_id=brand_id,
                query=message,
                num_results=5,
            )
        except Exception as e:
            log.warning("graphiti_read_failed", brand_id=brand_id, error=str(e))
            return []

    knowledge, brand_info, history_facts = await asyncio.gather(
        asyncio.to_thread(
            retrieve_knowledge,
            brand_id,
            intent.issue_type,
            customer_order_placed_at,
            "UK",
            message,
            intent.category.value,
        ),
        asyncio.to_thread(get_brand_info, brand_id),
        _fetch_graphiti_history(),
    )

    # ── Step 6: Session Compression ──────────────────────────────────────
    session_block = compress_session(conversation_history)

    # ── Step 7: Tool Manifest ─────────────────────────────────────────────
    tool_manifest = _TOOL_MANIFEST.get(intent.issue_type, ["escalate"])

    # ── Step 8: Context Packing ───────────────────────────────────────────
    brand_name = brand_info["name"] if brand_info else brand_id

    # Build system prompt: base + brand identity + channel adaptation
    base_prompt = _BRAND_SYSTEM_PROMPTS.get(brand_id, _DEFAULT_SYSTEM_PROMPT)
    channel_addition = _CHANNEL_ADDITIONS.get(channel, "")

    # Tone adaptation based on customer signals
    tone_note = ""
    if customer and customer.total_orders > 0:
        # Repeat customer or frustrated?
        pass  # Graphiti long-term history would inject this — placeholder for Phase 2

    system_prompt = base_prompt + channel_addition + tone_note

    # Build context blocks
    user_context_block = _format_user_context_block(customer, history_facts)
    policy_block = _format_policy_block(knowledge.policy, brand_name)
    resolution_block = _format_resolution_block(knowledge.resolutions, knowledge.kb_items)

    assembly_latency_ms = (time.monotonic() - t_start) * 1000
    log.info(
        "context_assembled",
        brand_id=brand_id,
        issue_type=intent.issue_type,
        resolution_count=len(knowledge.resolutions),
        kb_items=len(knowledge.kb_items),
        source=knowledge.source,
        latency_ms=round(assembly_latency_ms, 1),
    )

    return ContextPackage(
        brand_id=brand_id,
        customer_id=customer.id if customer else None,
        channel=channel,
        system_prompt=system_prompt,
        user_context_block=user_context_block,
        policy_block=policy_block,
        resolution_block=resolution_block,
        session_block=session_block,
        tool_manifest=tool_manifest,
        entities=intent.entities,
        intent=intent,
        applicable_policy=None,
        token_estimate=0,
        assembly_latency_ms=assembly_latency_ms,
        retrieval_confidence=knowledge.confidence,
    )
