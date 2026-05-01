"""
Eight specialist support agents covering the main L1 issue categories.

Prompts are generated dynamically per-brand using:
  - Knowledge Graph data (policies, brand metadata)
  - Domain config YAML (domains/{domain}/config.yaml)

The agent factories (get_*_agent) return agents WITHOUT system prompts.
run_specialist() injects the dynamically-generated prompt at call time.
This means a new brand with different return policies gets correct prompts
automatically — no code changes needed.

Agents:
  WISMO       — Where Is My Order (tracking, delivery ETA)
  Returns     — Return initiation, label generation
  Refund      — Refund status, delay escalation
  WrongItem   — Wrong/damaged item resolution
  Payment     — Payment failure, Klarna, alternative methods
  Promos      — Promo code validation, credits
  Account     — Account locked, password reset, order merge
  Escalation  — Final human handoff, ticket creation
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import structlog

from core.agents.base import build_agent
from core.tools.registry import (
    order_lookup,
    shipment_track,
    return_init,
    refund_status,
    cancel_order,
    email_label,
    promo_validate,
    size_guide,
    account_status,
    escalate,
    faq_search,
    policy_search,
)

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Lazy agent factory — no system prompt (prompt injected at call time)
# ═══════════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=None)
def get_wismo_agent():
    return build_agent(
        name="WISMO-Agent",
        description="Tracks orders and resolves delivery queries",
        system_prompt="",  # injected dynamically
        tools=[order_lookup, shipment_track, escalate],
    )


@lru_cache(maxsize=None)
def get_returns_agent():
    return build_agent(
        name="Returns-Agent",
        description="Initiates returns and sends prepaid labels",
        system_prompt="",
        tools=[order_lookup, return_init, email_label, escalate],
    )


@lru_cache(maxsize=None)
def get_refund_agent():
    return build_agent(
        name="Refund-Agent",
        description="Checks refund status and resolves delays",
        system_prompt="",
        tools=[order_lookup, refund_status, escalate],
    )


@lru_cache(maxsize=None)
def get_wrong_item_agent():
    return build_agent(
        name="WrongItem-Agent",
        description="Resolves wrong/damaged/missing item complaints",
        system_prompt="",
        tools=[order_lookup, shipment_track, return_init, email_label, escalate],
    )


@lru_cache(maxsize=None)
def get_payment_agent():
    return build_agent(
        name="Payment-Agent",
        description="Resolves payment failures and billing queries",
        system_prompt="",
        tools=[order_lookup, refund_status, escalate],
    )


@lru_cache(maxsize=None)
def get_promos_agent():
    return build_agent(
        name="Promos-Agent",
        description="Validates promo codes and resolves discount issues",
        system_prompt="",
        tools=[promo_validate, escalate],
    )


@lru_cache(maxsize=None)
def get_account_agent():
    return build_agent(
        name="Account-Agent",
        description="Manages account access, locks, and profile issues",
        system_prompt="",
        tools=[order_lookup, account_status, escalate],
    )


@lru_cache(maxsize=None)
def get_info_agent():
    return build_agent(
        name="Info-Agent",
        description="Answers FAQs and policy questions",
        system_prompt="",
        tools=[faq_search, policy_search, escalate],
    )


@lru_cache(maxsize=None)
def get_escalation_agent():
    return build_agent(
        name="Escalation-Agent",
        description="Creates L2 tickets and manages human handoff",
        system_prompt="",
        tools=[order_lookup, refund_status, escalate],
    )


# ── Intent → agent factory mapping ─────────────────────────────────────────────

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
    # Telecom fallbacks
    "network_outage": get_wismo_agent,
    "billing_dispute": get_refund_agent,
    "device_issue": get_wrong_item_agent,
}


def get_agent_for_intent(intent: str) -> Any:
    """Return the appropriate specialist agent for a given intent slug."""
    factory = INTENT_TO_AGENT.get(intent, get_info_agent)
    return factory()
