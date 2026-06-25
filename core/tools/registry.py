"""
Tool registry — all agent tools are defined here as async functions.

Each tool follows the LangChain/LangGraph tool pattern.
Tools are brand-scoped — they receive brand_id and enforce data isolation.
Execution is dynamically routed through the active ToolBackend (mock, stub, or live).
"""

from __future__ import annotations

from typing import Any
import structlog
from langchain_core.tools import tool

from core.tools.backends import get_tool_backend
from core.tools.backends.mock_backend import MockBackend

log = structlog.get_logger(__name__)

# To maintain backward compatibility with api.py administrative endpoints:
backend = get_tool_backend()
if isinstance(backend, MockBackend):
    _MOCK_ORDERS = backend._MOCK_ORDERS
    _MOCK_CUSTOMERS = backend._MOCK_CUSTOMERS
    _MOCK_RETURNS = backend._MOCK_RETURNS
    _MOCK_PROMOS = backend._MOCK_PROMOS
else:
    # If using stub/live backend, api.py still works with a separate mock structure.
    _compat_mock = MockBackend()
    _MOCK_ORDERS = _compat_mock._MOCK_ORDERS
    _MOCK_CUSTOMERS = _compat_mock._MOCK_CUSTOMERS
    _MOCK_RETURNS = _compat_mock._MOCK_RETURNS
    _MOCK_PROMOS = _compat_mock._MOCK_PROMOS


# ═══════════════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════

@tool
def order_lookup(order_id: str, brand_id: str) -> dict[str, Any]:
    """
    Look up the status and details of a customer's order.
    Returns order status, items, and basic shipment info.
    """
    return get_tool_backend().lookup_order(order_id, brand_id)


@tool
def shipment_track(order_id: str, brand_id: str) -> dict[str, Any]:
    """
    Retrieve detailed shipment tracking information for an order.
    Returns carrier, tracking ID, current status, and ETA.
    """
    return get_tool_backend().track_shipment(order_id, brand_id)


@tool
def return_init(
    order_id: str,
    brand_id: str,
    items_to_return: list[str],
    return_reason: str = "not_specified",
) -> dict[str, Any]:
    """
    Initiate a return for one or more items from an order.
    Returns a return reference number and instructions.
    """
    return get_tool_backend().initiate_return(order_id, brand_id, items_to_return, return_reason)


@tool
def refund_status(return_ref: str | None = None, order_id: str | None = None) -> dict[str, Any]:
    """
    Check the status of a refund for a given return reference or order ID.
    Returns refund status and expected processing date.
    """
    return get_tool_backend().check_refund_status(return_ref, order_id)


@tool
def cancel_order(order_id: str, brand_id: str) -> dict[str, Any]:
    """
    Attempt to cancel an order. Only possible while status is 'processing'.
    Returns success/failure and the reason if cancellation is not possible.
    """
    return get_tool_backend().cancel_order(order_id, brand_id)


@tool
def email_label(
    order_id: str,
    brand_id: str,
    customer_email: str,
    return_ref: str,
) -> dict[str, Any]:
    """
    Send a returns label to the customer's email address.
    """
    return get_tool_backend().email_label(order_id, brand_id, customer_email, return_ref)


@tool
def promo_validate(promo_code: str, brand_id: str, basket_value: float = 0.0) -> dict[str, Any]:
    """
    Validate whether a promotional/discount code is active and applicable.
    Returns validity status, discount details, and any restrictions.
    """
    return get_tool_backend().validate_promo(promo_code, brand_id, basket_value)


@tool
def faq_search(query: str, brand_id: str) -> dict[str, Any]:
    """
    Search the brand's FAQ knowledge base for answers to general questions.

    Use this when customers ask about:
    - How to contact us, opening hours
    - How to track, return, or exchange an order
    - Account help (reset password, update details)
    - Sizing, stock availability, sale info
    - Payment methods accepted
    - Delivery times and costs
    """
    return get_tool_backend().search_faqs(query, brand_id)


@tool
def policy_search(policy_type: str, brand_id: str) -> dict[str, Any]:
    """
    Retrieve the brand's policy for a specific topic.

    policy_type: one of "return", "delivery", "payment", "account", "privacy", "sizing"
    """
    return get_tool_backend().search_policies(policy_type, brand_id)


@tool
def size_guide(product_category: str, brand_id: str) -> dict[str, Any]:
    """
    Retrieve size guide information for a product category.
    Returns UK/US/EU size mappings.
    """
    return get_tool_backend().get_size_guide(product_category, brand_id)


@tool
def account_status(customer_id: str, brand_id: str) -> dict[str, Any]:
    """Check if a customer's account is active, suspended, or locked."""
    return get_tool_backend().check_account_status(customer_id, brand_id)


@tool
def escalate(
    session_id: str,
    brand_id: str,
    reason: str,
    priority: str = "P2",
    customer_id: str | None = None,
) -> dict[str, Any]:
    """
    Escalate the session to a human agent by creating a support ticket.
    Creates a Chatwoot ticket and notifies the agent queue.
    """
    return get_tool_backend().escalate_session(session_id, brand_id, reason, priority, customer_id)


# ── Tool registry — maps tool name to function ────────────────────────────
TOOL_REGISTRY: dict[str, Any] = {
    "order_lookup": order_lookup,
    "shipment_track": shipment_track,
    "return_init": return_init,
    "refund_status": refund_status,
    "cancel_order": cancel_order,
    "email_label": email_label,
    "promo_validate": promo_validate,
    "faq_search": faq_search,
    "policy_search": policy_search,
    "size_guide": size_guide,
    "account_status": account_status,
    "escalate": escalate,
    # Aliases
    "policy_lookup": order_lookup,    # policy is graph-retrieved; this is a no-op alias
    "refund_initiate": return_init,   # same flow as return_init for now
    "catalog_lookup": size_guide,     # simplified — full catalog lookup in Phase 2
}

ALL_TOOLS = list(TOOL_REGISTRY.values())
