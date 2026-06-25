"""
Domain-agnostic specialist support agents resolved dynamically from domain configurations.

Prompts, name, description, and tools are generated dynamically per-brand using:
  - Knowledge Graph data (policies, brand metadata)
  - Domain config YAML (domains/{domain}/config.yaml)

Backward-compatible helper functions are provided for test suites.
"""

from __future__ import annotations

from typing import Any
import structlog

from core.agents.base import build_agent
from core.tools.registry import TOOL_REGISTRY

log = structlog.get_logger(__name__)

_FALLBACK_SPECIALIST_MAP: dict[str, str] = {
    "wismo": "wismo_agent",
    "return_initiate": "returns_agent",
    "refund_delay": "refund_agent",
    "wrong_item": "wrong_item_agent",
    "damaged_item": "wrong_item_agent",
    "order_cancel": "returns_agent",
    "payment_failed": "payment_agent",
    "klarna_return": "refund_agent",
    "afterpay_return": "refund_agent",
    "promo_not_applied": "promos_agent",
    "size_query": "account_agent",
    "account_locked": "account_agent",
    "delivery_query": "wismo_agent",
    "info": "info_agent",
    "network_outage": "wismo_agent",
    "billing_dispute": "refund_agent",
    "device_issue": "wrong_item_agent",
}


def get_agent_for_intent(intent: str, brand_id: str | None = None) -> Any:
    """
    Return the appropriate specialist agent dynamically resolved from the
    active brand's domain config and tool manifest.
    """
    from brands.registry import get_brand_by_id
    from domains.loader import get_domain_config

    # Resolve domain from brand, defaulting to "ecommerce"
    domain = "ecommerce"
    if brand_id:
        brand_entry = get_brand_by_id(brand_id)
        if brand_entry and brand_entry.domain:
            domain = brand_entry.domain

    domain_cfg = get_domain_config(domain)

    # Get specialist name mapped to this intent, e.g. "wismo_agent"
    agent_id = domain_cfg.get_specialist_for(intent) or _FALLBACK_SPECIALIST_MAP.get(intent, "info_agent")

    # Retrieve specialist agent specification
    spec = domain_cfg.specialist_agents.get(agent_id)
    name = spec.name if spec else agent_id.replace("_", "-").title()
    description = spec.description if spec else f"Specialist for {intent}"

    # Resolve tools for this intent from the domain tool manifest
    tool_names = domain_cfg.tool_manifest.get(intent, [])
    agent_tools = []
    for t_name in tool_names:
        if t_name in TOOL_REGISTRY:
            agent_tools.append(TOOL_REGISTRY[t_name])

    # Always include escalate tool
    if "escalate" in TOOL_REGISTRY and TOOL_REGISTRY["escalate"] not in agent_tools:
        agent_tools.append(TOOL_REGISTRY["escalate"])

    return build_agent(
        name=name,
        description=description,
        system_prompt="",
        tools=agent_tools,
    )


# ── Backward Compatibility Helpers ──────────────────────────────────────────

def get_wismo_agent() -> Any:
    return get_agent_for_intent("wismo")

def get_returns_agent() -> Any:
    return get_agent_for_intent("return_initiate")

def get_refund_agent() -> Any:
    return get_agent_for_intent("refund_delay")

def get_wrong_item_agent() -> Any:
    return get_agent_for_intent("wrong_item")

def get_payment_agent() -> Any:
    return get_agent_for_intent("payment_failed")

def get_promos_agent() -> Any:
    return get_agent_for_intent("promo_not_applied")

def get_account_agent() -> Any:
    return get_agent_for_intent("account_locked")

def get_info_agent() -> Any:
    return get_agent_for_intent("info")

def get_escalation_agent() -> Any:
    return get_agent_for_intent("escalate")


INTENT_TO_AGENT = {
    "wismo": get_wismo_agent,
    "return_initiate": get_returns_agent,
    "refund_delay": get_refund_agent,
    "wrong_item": get_wrong_item_agent,
    "damaged_item": get_wrong_item_agent,
    "order_cancel": get_returns_agent,
    "payment_failed": get_payment_agent,
    "klarna_return": get_refund_agent,
    "promo_not_applied": get_promos_agent,
    "size_query": get_account_agent,
    "account_locked": get_account_agent,
    "escalate": get_escalation_agent,
    "info": get_info_agent,
}
