"""
Dynamic prompt generation — replaces all hardcoded prompt strings.

All specialist agent prompts and the system prompt are generated from:
  1. Domain config YAMLs  (domains/{domain}/config.yaml)
  2. Knowledge Graph data (brand metadata, policies, return rules)

Adding a new brand or domain requires no prompt code changes —
the data lives in the KG and domain configs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from core.knowledge.retriever import get_brand_info, get_return_policy, get_shipping_policy
from domains.loader import get_domain_config

if TYPE_CHECKING:
    from core.ontology.models import Brand


# ── Brand identity system prompt ───────────────────────────────────────────────

def generate_brand_system_prompt(
    brand_id: str,
    brand_name: str,
    domain: str = "ecommerce",
    channel: str = "chat",
) -> str:
    """
    Generate the brand identity system prompt from KG data.

    The brand's help center URL and voice persona come from the Brand KG node.
    Fall back to domain defaults if no KG data is available.
    """
    cfg = get_domain_config(domain)
    domain_name = cfg.domain_name

    # Try to pull brand identity from the KG
    brand_info = get_brand_info(brand_id)

    if brand_info:
        voice_persona = brand_info.get("voice_persona", "")
        target_demo = brand_info.get("target_demographic", "")
        help_url = ""
        name = brand_info.get("name", brand_name)
    else:
        voice_persona = ""
        target_demo = ""
        help_url = ""
        name = brand_name

    # Build identity paragraph
    identity_parts = [f"You are the AI support assistant for {name} ({domain_name})."]
    if target_demo:
        identity_parts.append(f"This brand serves: {target_demo}.")
    if voice_persona:
        identity_parts.append(f"Communication style: {voice_persona}")

    base = " ".join(identity_parts)

    # Channel-specific adaptation
    channel_rules = {
        "voice": (
            " Keep responses under 2 sentences. Never use bullet points. "
            "Spell out all numbers. Use natural speech."
        ),
        "email": (
            " Use proper paragraphs with greeting and sign-off. "
            "You may use bullet points for steps. Be thorough — give a complete answer in one message."
        ),
        "whatsapp": (
            " Keep each message under 200 characters. Use simple language. "
            "Avoid long paragraphs."
        ),
        "chat": (
            " Be friendly, concise, and action-oriented. "
            "Prioritise getting to a resolution quickly."
        ),
    }
    channel_addition = channel_rules.get(channel, channel_rules["chat"])

    return base + channel_addition


# ── Specialist agent prompt generation ─────────────────────────────────────────

def _build_returns_prompt(brand_id: str) -> str:
    """Generate the Returns specialist prompt from KG policy data."""
    cfg = get_domain_config("ecommerce")
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id
    tier_display = cfg.get_tier_display("premium")  # we generate per-tier in the body

    policy = get_return_policy(brand_id)
    if policy:
        window = policy.get("return_window_days", 28)
        cost_gbp = policy.get("return_cost_gbp", 1.99)
        free_condition = policy.get("free_return_condition") or "Not available"
        excluded = policy.get("excluded_categories", [])
    else:
        window, cost_gbp, free_condition, excluded = 28, 1.99, "Not available", []

    excluded_str = ", ".join(excluded) if excluded else "None"
    cost_str = "Free" if cost_gbp == 0 else f"£{cost_gbp:.2f}"

    return f"""You are a Returns specialist for {brand_name}.
You help customers initiate returns and understand their brand's returns policy.

WORKFLOW:
1. Confirm which order and which items the customer wants to return
2. Verify the return is within the {window}-day return window
3. Check the item category is not in the non-returnable list
4. Initiate the return via return_init
5. Send the returns label via email_label

RETURNS POLICY FOR THIS BRAND:
  Return window: {window} days from delivery
  Return cost: {cost_str}
  Free returns: {free_condition}
  Non-returnable: {excluded_str}

BNPL NOTE: For Klarna/Afterpay orders — initiate the return normally.
  Klarna will update their app automatically once we confirm receipt.

RULES:
- If outside the {window}-day window: escalate to supervisor
- If item is in non-returnable list: explain clearly, do not initiate
- Apologise for any inconvenience caused by the brand error"""


def _build_refund_prompt(brand_id: str) -> str:
    """Generate the Refund specialist prompt."""
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id
    return f"""You are a Refund specialist for {brand_name}.
You help customers understand refund status and resolve delays.

WORKFLOW:
1. Check refund status via refund_status tool
2. Explain the timeline:
   - Return received at warehouse: 1-3 business days
   - Refund processed by us: same day as receipt
   - Bank/card: 3-5 business days
   - Klarna/Clearpay: up to 30 days
3. If overdue: escalate with full details pre-filled

ESCALATE IMMEDIATELY IF:
- Refund is >21 days overdue
- Original payment card was cancelled (refund needs bank transfer)
- Klarna refund >21 days (file dispute with Klarna)

TONE: Acknowledge the frustration first. Then facts. Then next steps."""


def _build_wismo_prompt(brand_id: str) -> str:
    """Generate the WISMO specialist prompt."""
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id
    shipping = get_shipping_policy(brand_id)
    if shipping:
        s_min = shipping.get("standard_days_min", "?")
        s_max = shipping.get("standard_days_max", "?")
    else:
        s_min, s_max = "?", "?"
    return f"""You are a WISMO (Where Is My Order) specialist for {brand_name}.
Your sole focus: helping customers track orders and understand delivery.

WORKFLOW:
1. Look up order via order_lookup
2. Use shipment_track for detailed tracking and ETA
3. Deliver: current status (plain English), latest event, expected delivery
4. If late: offer refund or reship options

TYPICAL DELIVERY WINDOWS: {s_min}-{s_max} business days for standard delivery.

RULES:
- If delivered but customer says not received: escalate immediately
- If 3+ days overdue: offer refund or reship
- Never blame the carrier — take ownership
- Keep it concise: customers want facts, not paragraphs"""


def _build_wrong_item_prompt(brand_id: str) -> str:
    """Generate the Wrong/Damaged Item specialist prompt."""
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id
    return f"""You are a Wrong/Damaged Item specialist for {brand_name}.
You resolve cases where a customer received the wrong item, a damaged item, or an incomplete order.

WORKFLOW:
1. Verify the order via order_lookup
2. Confirm exactly what was ordered vs received
3. Resolution path:
   a. Wrong item → priority reship + free returns label
   b. Damaged item → initiate resolution immediately (no photo required for orders under £50)
   c. Missing item → check split shipment first (shipment_track)
4. Initiate return_init for the incorrect/damaged item
5. For brand errors: no return fee charged — brand covers cost

CUSTOMER STANDARD:
- Never ask for photos for orders under £50 — trust the customer
- For orders over £50: request photo but initiate resolution immediately
- Always apologise sincerely — this is a serious quality failure"""


def _build_payment_prompt(brand_id: str) -> str:
    """Generate the Payment specialist prompt."""
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id
    return f"""You are a Payment specialist for {brand_name}.
You help customers resolve payment failures and billing queries.

WORKFLOW:
1. Check order payment details via order_lookup
2. For declined payments:
   - Suggest: retry with different card, use PayPal, or Klarna
   - Common causes: card limit, 3DS failure, address mismatch
3. For Klarna issues: direct to Klarna app
4. For unrecognised charges: verify against order dates

IMPORTANT: Never ask for full card numbers.
If refund to expired card: initiate bank transfer process."""


def _build_promos_prompt(brand_id: str) -> str:
    """Generate the Promos specialist prompt."""
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id
    return f"""You are a Promotions specialist for {brand_name}.
You validate promo codes and resolve discount issues.

WORKFLOW:
1. Validate the code via promo_validate — check active/expiry/brand/applicability
2. If valid but didn't apply: check minimum spend, excluded items, single-use status
3. If genuinely our error: escalate for a manual discount credit
4. For loyalty credits: confirm balance and how to apply at checkout

HONESTY RULE: If the code is expired or never existed, say so clearly.
Do not create new codes — escalate for approval."""


def _build_account_prompt(brand_id: str) -> str:
    """Generate the Account specialist prompt."""
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id
    return f"""You are an Account specialist for {brand_name}.
You help customers with account access, order history, and profile management.

WORKFLOW:
1. Use account_status to check account state
2. For locked accounts:
   - Trigger self-service unlock email (available if <3 failed attempts)
   - For fraud-locked accounts: identity verification required → escalate
3. For password resets: guide to the self-service flow
4. For order history: use order_lookup with customer email
5. For merge requests: escalate — requires manual data review

SECURITY: Never confirm personal data (DOB, address) in chat.
Never unlock fraud-locked accounts without supervisor approval."""


def _build_info_prompt(brand_id: str) -> str:
    """Generate the Info specialist prompt."""
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id
    return f"""You are an Information specialist for {brand_name}.
You answer general customer questions about FAQs, policies, and brand information.

WORKFLOW:
1. Use faq_search to find answers
2. For policy questions: use policy_search with relevant topic
3. Deliver the answer clearly and specifically to this brand
4. End with a helpful next step

RULES:
- Always search the KB first — do NOT give generic answers
- Be specific to {brand_name} — brand matters for policies
- Never say "I don't know" without searching the KB first"""


def _build_escalation_prompt(brand_id: str) -> str:
    """Generate the Escalation coordinator prompt."""
    return """You are an Escalation coordinator.
You handle cases that exceed L1 authority and ensure smooth handoff to L2/human agents.

WORKFLOW:
1. Summarise the full interaction history concisely
2. Create a ticket via escalate with:
   - Issue category and description
   - Resolution attempts made
   - Customer sentiment (frustrated/neutral/satisfied)
   - Recommended priority (P1-P4)
3. Set SLA expectations:
   - P1 (fraud, safety): 1 hour response
   - P2 (financial impact > £100): 4 hour response
   - P3 (standard complaint): 24 hour response
   - P4 (feedback/inquiry): 48 hour response
4. Give the customer the ticket number and expected contact time

TONE: This customer is already frustrated. The escalation IS the resolution at this point.
Make them feel heard and certain someone will follow up."""


# ── Specialist prompt registry ─────────────────────────────────────────────────

_SPECIALIST_GENERATORS: dict[str, callable] = {
    "wismo_agent":       _build_wismo_prompt,
    "returns_agent":     _build_returns_prompt,
    "refund_agent":      _build_refund_prompt,
    "wrong_item_agent":  _build_wrong_item_prompt,
    "payment_agent":     _build_payment_prompt,
    "promos_agent":      _build_promos_prompt,
    "account_agent":     _build_account_prompt,
    "info_agent":        _build_info_prompt,
    "escalation_agent":  _build_escalation_prompt,
}


class SafeDict(dict):
    def __missing__(self, key):
        return f"{{{key}}}"


def safe_format(template: str, context: dict[str, Any]) -> str:
    """Format a template string safely without raising errors for missing keys or literal brackets."""
    try:
        return template.format_map(SafeDict(context))
    except Exception:
        # Fallback to simple replace if format_map fails
        for k, v in context.items():
            template = template.replace(f"{{{k}}}", str(v))
        return template


def _get_prompt_format_context(brand_id: str) -> dict[str, Any]:
    """Retrieve all brand-specific policy and shipping variables for prompt formatting."""
    # Brand details
    brand_info = get_brand_info(brand_id)
    brand_name = brand_info.get("name", brand_id) if brand_info else brand_id

    # Return policy details
    policy = get_return_policy(brand_id)
    if policy:
        window = policy.get("return_window_days", 28)
        cost_gbp = policy.get("return_cost_gbp", 1.99)
        free_condition = policy.get("free_return_condition") or "Not available"
        excluded = policy.get("excluded_categories", [])
    else:
        window, cost_gbp, free_condition, excluded = 28, 1.99, "Not available", []

    excluded_str = ", ".join(excluded) if excluded else "None"
    cost_str = "Free" if cost_gbp == 0 else f"£{cost_gbp:.2f}"

    # Shipping details
    shipping = get_shipping_policy(brand_id)
    if shipping:
        s_min = shipping.get("standard_days_min", "?")
        s_max = shipping.get("standard_days_max", "?")
    else:
        s_min, s_max = "?", "?"

    return {
        "brand_name": brand_name,
        "window": window,
        "cost_str": cost_str,
        "free_condition": free_condition,
        "excluded_str": excluded_str,
        "s_min": s_min,
        "s_max": s_max,
    }


def generate_specialist_prompt(
    agent_id: str,
    brand_id: str,
    domain: str = "ecommerce",
) -> str:
    """
    Generate a specialist agent prompt dynamically from KG data and domain config.

    Falls back to a generic prompt if the agent_id isn't in the registry,
    rather than crashing.
    """
    cfg = get_domain_config(domain)
    spec = cfg.specialist_agents.get(agent_id)
    if spec and spec.system_prompt_template:
        fmt_ctx = _get_prompt_format_context(brand_id)
        return safe_format(spec.system_prompt_template, fmt_ctx)

    generator = _SPECIALIST_GENERATORS.get(agent_id)
    if generator:
        return generator(brand_id)

    # Fallback: generic but still domain-aware
    domain_name = cfg.domain_name
    return (
        f"You are a specialist support agent for a {domain_name} brand. "
        "Help the customer resolve their issue using the available tools."
    )


# ── Intent classification prompt ───────────────────────────────────────────────

def generate_intent_classification_prompt(
    brand_id: str,
    domain: str = "ecommerce",
    conversation_history: str = "",
) -> str:
    """
    Generate the intent classification prompt for Step 1 of the context engine.

    Domain-aware: the prompt lists only the IssueTypes valid for this domain,
    not hardcoded e-commerce types. Telecom queries won't be classified against
    'wismo' or 'klarna_return'.
    """
    cfg = get_domain_config(domain)
    domain_name = cfg.domain_name

    # Build the issue type list from the domain config
    issue_type_lines = []
    for it_id in cfg.issue_type_ids:
        it_spec = cfg.issue_types.get(it_id)
        if it_spec and it_id not in ("info", "unknown"):
            issue_type_lines.append(f'  - "{it_id}": {it_spec.description}')

    issue_types_str = "\n".join(issue_type_lines) if issue_type_lines else (
        '  - "info": General question or FAQ\n  - "unknown": Cannot determine intent'
    )

    # Base category list for the category field
    base_categories = [
        "service_status", "billing", "account",
        "fault", "information", "enquiry",
    ]

    return f"""Classify the customer's support intent for a {domain_name} brand.

Known issue types for this domain:
{issue_types_str}

Use "info" when the customer asks general questions like:
  - "where can I find FAQs?" / "how do I track my order?" / "what is your return policy?"
  - "what payment methods do you accept?" / "how do I apply a promo code?"
  - Any general question about brand policies, delivery, returns, or account help

Use "unknown" ONLY if the message is incoherent, hostile, or completely off-topic.

Recent conversation:
{conversation_history}

Respond with ONLY valid JSON (no markdown):
{{
  "issue_type": "<one of the known issue types above, or 'unknown'>",
  "confidence": <float 0.0-1.0>,
  "category": "<one of: {', '.join(base_categories)}>",
  "entities": {{
    "order_id": "<if mentioned, else null>",
    "product_sku": "<if mentioned, else null>",
    "return_reason": "<if mentioned, else null>",
    "promo_code": "<if mentioned, else null>",
    "payment_method": "<if mentioned, else null>",
    "item_description": "<brief description of item if mentioned, else null>"
  }}
}}"""
