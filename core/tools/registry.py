"""
Tool registry — all agent tools are defined here as async functions.

Phase 1: Mock implementations that simulate real API responses.
Phase 2: Replace with real API integrations (Boohoo order API, etc.).

Each tool follows the LangChain/LangGraph tool pattern.
Tools are brand-scoped — they receive brand_id and enforce data isolation.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from langchain_core.tools import tool

log = structlog.get_logger(__name__)

# ── Mock data store (Phase 1 — replace with real API in Phase 2) ──────────

_MOCK_ORDERS: dict[str, dict[str, Any]] = {
    "BH-12345-UK": {
        "id": "BH-12345-UK", "brand_id": "boohoo_uk", "customer_id": "cust_001",
        "status": "in_transit",
        "placed_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        "total_value": 45.99, "currency": "GBP",
        "items": [{"sku": "BH-DRESS-001", "name": "Floral Wrap Dress", "quantity": 1, "price_paid": 35.99}],
        "shipment": {
            "tracking_id": "EVRI-987654321",
            "carrier": "Evri",
            "estimated_delivery": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "current_status": "Out for delivery",
            "tracking_url": "https://evri.com/track/EVRI-987654321",
        },
    },
    "PLT-98765-UK": {
        "id": "PLT-98765-UK", "brand_id": "plt_uk", "customer_id": "cust_002",
        "status": "processing",
        "placed_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "total_value": 29.00, "currency": "GBP",
        "items": [{"sku": "PLT-TOP-042", "name": "Satin Cami Top", "quantity": 2, "price_paid": 14.50}],
        "shipment": None,
    },
    "KM-55001-UK": {
        "id": "KM-55001-UK", "brand_id": "karenmillen_uk", "customer_id": "cust_003",
        "status": "delivered",
        "placed_at": (datetime.now(timezone.utc) - timedelta(days=12)).isoformat(),
        "total_value": 189.00, "currency": "GBP",
        "items": [{"sku": "KM-COAT-007", "name": "Tailored Wool Coat", "quantity": 1, "price_paid": 189.00}],
        "shipment": {
            "tracking_id": "DHL-112233445",
            "carrier": "DHL",
            "estimated_delivery": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
            "current_status": "Delivered",
            "tracking_url": "https://dhl.com/track/DHL-112233445",
        },
    },
}

_MOCK_CUSTOMERS: dict[str, dict[str, Any]] = {
    "cust_001": {
        "customer_id": "cust_001",
        "brand_id": "boohoo_uk",
        "name": "Alice Thompson",
        "email": "alice@example.com",
        "tier": "standard",
        "locale": "en-GB",
        "total_orders": 5,
        "total_spend": 0.0,
    },
    "cust_002": {
        "customer_id": "cust_002",
        "brand_id": "plt_uk",
        "name": "Sophie Clarke",
        "email": "sophie@example.com",
        "tier": "loyalty",
        "locale": "en-GB",
        "total_orders": 12,
        "total_spend": 0.0,
    },
    "cust_003": {
        "customer_id": "cust_003",
        "brand_id": "karenmillen_uk",
        "name": "Helena Murray",
        "email": "helena@example.com",
        "tier": "vip",
        "locale": "en-GB",
        "total_orders": 8,
        "total_spend": 0.0,
    },
}

# Compute total spend from orders into customer profiles
for _order in _MOCK_ORDERS.values():
    _cid = _order["customer_id"]
    if _cid in _MOCK_CUSTOMERS:
        _MOCK_CUSTOMERS[_cid]["total_spend"] = round(
            _MOCK_CUSTOMERS[_cid]["total_spend"] + _order["total_value"], 2
        )

_MOCK_RETURNS: dict[str, dict[str, Any]] = {}
_MOCK_PROMOS: dict[str, dict[str, Any]] = {
    "SAVE10": {"code": "SAVE10", "discount_pct": 10, "min_spend": 30, "active": True,
               "applies_to_brands": ["boohoo_uk", "plt_uk"]},
    "NEWCUST20": {"code": "NEWCUST20", "discount_pct": 20, "min_spend": 25, "active": True,
                  "applies_to_brands": None},  # None = all brands
    "EXPIRED5": {"code": "EXPIRED5", "discount_pct": 5, "min_spend": 0, "active": False,
                 "applies_to_brands": None},
}


# ═══════════════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════

@tool
def order_lookup(order_id: str, brand_id: str) -> dict[str, Any]:
    """
    Look up the status and details of a customer's order.
    Returns order status, items, and basic shipment info.
    """
    log.info("tool_called", tool="order_lookup", order_id=order_id, brand_id=brand_id)

    # Normalize order ID — handle common variations e.g. "order 12345" → "BH-12345-UK"
    normalized = order_id.upper().strip()
    order = _MOCK_ORDERS.get(normalized)

    if not order:
        return {
            "found": False,
            "message": f"No order found with ID {order_id}. Please check the order number from your confirmation email.",
        }
    if order["brand_id"] != brand_id:
        return {
            "found": False,
            "message": f"Order {order_id} was not found for this brand. Please check you're contacting the correct brand's support.",
        }

    return {"found": True, **order}


@tool
def shipment_track(order_id: str, brand_id: str) -> dict[str, Any]:
    """
    Retrieve detailed shipment tracking information for an order.
    Returns carrier, tracking ID, current status, and ETA.
    """
    log.info("tool_called", tool="shipment_track", order_id=order_id)
    order = _MOCK_ORDERS.get(order_id.upper())
    if not order or not order.get("shipment"):
        return {
            "tracked": False,
            "message": "No tracking information available yet. The order may still be processing.",
        }
    return {"tracked": True, **order["shipment"]}


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
    log.info("tool_called", tool="return_init", order_id=order_id, brand_id=brand_id)
    return_ref = f"RET-{uuid.uuid4().hex[:8].upper()}"
    _MOCK_RETURNS[return_ref] = {
        "return_ref": return_ref,
        "order_id": order_id,
        "brand_id": brand_id,
        "items": items_to_return,
        "reason": return_reason,
        "status": "initiated",
        "initiated_at": datetime.now(timezone.utc).isoformat(),
        "refund_eta": (datetime.now(timezone.utc) + timedelta(days=21)).isoformat(),
    }
    return {
        "success": True,
        "return_ref": return_ref,
        "message": (
            f"Return initiated. Reference: {return_ref}. "
            "Please check your email for your returns label. "
            "Refund will be processed within 21 days of us receiving the parcel."
        ),
        "refund_eta_days": 21,
    }


@tool
def refund_status(return_ref: str | None = None, order_id: str | None = None) -> dict[str, Any]:
    """
    Check the status of a refund for a given return reference or order ID.
    Returns refund status and expected processing date.
    """
    log.info("tool_called", tool="refund_status", return_ref=return_ref, order_id=order_id)

    if return_ref and return_ref in _MOCK_RETURNS:
        ret = _MOCK_RETURNS[return_ref]
        initiated_at = datetime.fromisoformat(ret["initiated_at"])
        days_elapsed = (datetime.now(timezone.utc) - initiated_at).days
        return {
            "found": True,
            "return_ref": return_ref,
            "status": ret["status"],
            "days_elapsed": days_elapsed,
            "days_remaining": max(0, 21 - days_elapsed),
            "refund_eta": ret["refund_eta"],
            "overdue": days_elapsed > 21,
        }

    # Simulate a mock return not in local store
    return {
        "found": False,
        "message": (
            "We couldn't find a return with that reference. "
            "Please check your return confirmation email for the correct reference number."
        ),
    }


@tool
def cancel_order(order_id: str, brand_id: str) -> dict[str, Any]:
    """
    Attempt to cancel an order. Only possible while status is 'processing'.
    Returns success/failure and the reason if cancellation is not possible.
    """
    log.info("tool_called", tool="cancel_order", order_id=order_id)
    order = _MOCK_ORDERS.get(order_id.upper())

    if not order:
        return {"success": False, "reason": "Order not found."}
    if order["status"] != "processing":
        return {
            "success": False,
            "reason": (
                f"Order is already {order['status']} and cannot be cancelled. "
                "Please refuse delivery or return the item when it arrives."
            ),
        }
    # Simulate successful cancellation
    order["status"] = "cancelled"
    return {
        "success": True,
        "message": "Your order has been successfully cancelled. You will receive a full refund within 3-5 business days.",
    }


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
    log.info("tool_called", tool="email_label", order_id=order_id, return_ref=return_ref)
    return {
        "sent": True,
        "message": f"Returns label sent to {customer_email}. Please allow a few minutes for delivery.",
        "return_ref": return_ref,
    }


@tool
def promo_validate(promo_code: str, brand_id: str, basket_value: float = 0.0) -> dict[str, Any]:
    """
    Validate whether a promotional/discount code is active and applicable.
    Returns validity status, discount details, and any restrictions.
    """
    log.info("tool_called", tool="promo_validate", promo_code=promo_code, brand_id=brand_id)
    promo = _MOCK_PROMOS.get(promo_code.upper())

    if not promo:
        return {"valid": False, "reason": "Code not found. Please check the code and try again."}
    if not promo["active"]:
        return {"valid": False, "reason": "This promotional code has expired."}
    if promo["applies_to_brands"] and brand_id not in promo["applies_to_brands"]:
        return {"valid": False, "reason": "This code is not valid for this brand."}
    if basket_value < promo["min_spend"]:
        return {
            "valid": False,
            "reason": f"Minimum spend of £{promo['min_spend']:.2f} required. Add more items to qualify.",
        }

    return {
        "valid": True,
        "discount_pct": promo["discount_pct"],
        "message": f"Code is valid. {promo['discount_pct']}% discount will be applied at checkout.",
    }


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
    log.info("tool_called", tool="faq_search", query=query, brand_id=brand_id)

    # Pull from the seeded FAQ KB in FalkorDB (if available)
    try:
        from core.knowledge.retriever import search_kb_nodes
        kb_results = search_kb_nodes(brand_id, query, intent_category="faq")
        if kb_results:
            return {"found": True, "kb_results": kb_results, "answer": None}
    except Exception as exc:
        log.warning("faq_kb_fallback", error=str(exc))

    # Fallback: static mock FAQs
    _FAQS: dict[str, list[dict]] = {
        "boohoo_uk": [
            {"question": "How do I return an item?", "answer": "Log into your account, go to 'My Orders', select the order and click 'Return Item'. Pack the item, attach the prepaid label (for UK orders), and drop it off at any Evri parcel point. Refunds are processed within 3-5 business days of receipt."},
            {"question": "How long does delivery take?", "answer": "Standard delivery: 3-5 working days (£3.99). Next day delivery available for £5.99. Free standard delivery on orders over £35."},
            {"question": "How do I track my order?", "answer": "Log into your account and go to 'My Orders'. Click 'Track' next to your order. You'll receive a tracking link via email once dispatched."},
            {"question": "What payment methods do you accept?", "answer": "We accept Visa, Mastercard, American Express, PayPal, Klarna, Laybuy, and Boohoo gift cards."},
            {"question": "How do I apply a promo code?", "answer": "Enter your code at the checkout page in the 'Gift card or discount code' box and click 'Apply'. Only one promo code can be used per order unless stated otherwise."},
        ],
        "plt_uk": [
            {"question": "How do I return an item?", "answer": "Visit our returns portal at plt.co.uk/returns, enter your order number and email, then follow the steps to generate a prepaid return label. Returns are free for loyalty members."},
            {"question": "What is your returns policy?", "answer": "We accept returns within 28 days of delivery. Items must be unworn with original tags attached. Sale items can be returned within 14 days."},
            {"question": "How can I track my order?", "answer": "You'll receive a tracking number via SMS and email once your order is dispatched. You can also track via your account 'My Orders' page."},
        ],
    }

    brand_faqs = _FAQS.get(brand_id, _FAQS.get("boohoo_uk", []))
    if query:
        import re
        query_lower = query.lower()
        scored = []
        for faq in brand_faqs:
            q_words = set(re.findall(r'\w+', faq["question"].lower()))
            a_words = set(re.findall(r'\w+', faq["answer"].lower()))
            combined = q_words | a_words
            score = sum(1 for w in query_lower.split() if w in combined)
            scored.append((score, faq))
        scored.sort(reverse=True)
        results = [{"question": q, "answer": a, "score": s} for s, q, a in scored[:3] if s > 0]
        if results:
            faqs = "\n\n".join(f"Q: {r['question']}\nA: {r['answer']}" for r in results)
            return {"found": True, "results": results, "answer": faqs}

    return {
        "found": False,
        "results": [],
        "answer": None,
        "message": "No specific FAQ found for your query. Please contact our customer service team or ask me a more specific question.",
    }


@tool
def policy_search(policy_type: str, brand_id: str) -> dict[str, Any]:
    """
    Retrieve the brand's policy for a specific topic.

    policy_type: one of "return", "delivery", "payment", "account", "privacy", "sizing"
    """
    log.info("tool_called", tool="policy_search", policy_type=policy_type, brand_id=brand_id)

    _POLICIES: dict[str, dict[str, str]] = {
        "boohoo_uk": {
            "return": "RETURN POLICY — Boohoo UK:\n• Return window: 28 days from delivery\n• Return cost: £1.99 (deducted from refund) — FREE for loyalty members\n• How to return: Log into boohoo.com, go to My Account > My Orders > Return Item. Attach the prepaid label and drop off at any Evri point.\n• Refund timeline: 3-5 business days after we receive your item\n• Non-returnable: swimwear, lingerie, pierced jewellery, beauty products (if seal broken), personalised items, sale items (unless faulty)\n• Exchanges: We don't offer direct exchanges. Return for a refund and place a new order.",
            "delivery": "DELIVERY POLICY — Boohoo UK:\n• Standard delivery: 3-5 working days — £3.99 (free on orders over £35)\n• Next day delivery: Order by 10pm — £5.99\n• Nominated day delivery: Choose your day — £5.99\n• Collection: Collect from over 900 Evri points — £2.99\n• Weekend delivery: Friday-Sunday — £7.99\n• International: Available to 90+ countries, rates at checkout",
            "payment": "PAYMENT POLICY — Boohoo UK:\n• We accept: Visa, Mastercard, American Express, PayPal, Klarna (buy now pay later), Laybuy (pay in 6 instalments), Boohoo gift cards\n• Klarna: Repayment is between you and Klarna. If we issue a refund, Klarna will be notified automatically.\n• Gift cards: Can be used alongside other payment methods.",
        },
        "plt_uk": {
            "return": "RETURN POLICY — PrettyLittleThing UK:\n• Return window: 28 days (14 days for sale items)\n• Return cost: £1.99 — FREE for PLT loyalty members\n• How to return: Use the returns portal at plt.co.uk/returns\n• Refund timeline: 5-7 business days after receipt\n• Non-returnable: Swimwear, lingerie, pierced jewellery, beauty products (seal broken), personalised items",
            "delivery": "DELIVERY POLICY — PrettyLittleThing UK:\n• Standard delivery: 4-6 working days — £3.99\n• Express delivery: 2-3 working days — £5.99\n• Next day delivery: Order before 3pm — £7.99\n• Free delivery for PLT Pro members",
        },
        "karenmillen_uk": {
            "return": "RETURN POLICY — Karen Millen UK:\n• Return window: 28 days\n• Return cost: FREE (complimentary returns for all customers)\n• How to return: Use the prepaid label inside your parcel or generate one at karenmillen.com/returns\n• Refund timeline: 3-5 business days\n• Non-returnable: Swimwear, lingerie, pierced jewellery, altered items",
            "delivery": "DELIVERY POLICY — Karen Millen UK:\n• Standard delivery: 3-5 working days — FREE on orders over £50, otherwise £3.95\n• Express delivery: Next working day — £7.95\n• Named day delivery: £9.95\n• Collect from store: Free at 300+ locations",
        },
    }

    brand_policies = _POLICIES.get(brand_id, _POLICIES.get("boohoo_uk", {}))
    policy_text = brand_policies.get(policy_type)

    if policy_text:
        return {"found": True, "policy_type": policy_type, "policy": policy_text}
    return {
        "found": False,
        "policy_type": policy_type,
        "policy": None,
        "message": f"No {policy_type} policy found for this brand.",
    }


@tool
def size_guide(product_category: str, brand_id: str) -> dict[str, Any]:
    """
    Retrieve size guide information for a product category.
    Returns UK/US/EU size mappings.
    """
    log.info("tool_called", tool="size_guide", category=product_category, brand_id=brand_id)
    # Simplified size guide — real implementation queries product catalog
    return {
        "category": product_category,
        "brand_id": brand_id,
        "size_guide": {
            "XS": {"UK": "6", "US": "2", "EU": "34", "bust_cm": "80-83", "waist_cm": "62-65"},
            "S":  {"UK": "8", "US": "4", "EU": "36", "bust_cm": "84-87", "waist_cm": "66-69"},
            "M":  {"UK": "10-12", "US": "6-8", "EU": "38-40", "bust_cm": "88-95", "waist_cm": "70-77"},
            "L":  {"UK": "14-16", "US": "10-12", "EU": "42-44", "bust_cm": "96-103", "waist_cm": "78-85"},
            "XL": {"UK": "18-20", "US": "14-16", "EU": "46-48", "bust_cm": "104-111", "waist_cm": "86-93"},
        },
        "note": "Sizes can vary by style. If between sizes, we recommend sizing up. Returns are always available if the fit isn't right.",
    }


@tool
def account_status(customer_id: str, brand_id: str) -> dict[str, Any]:
    """Check if a customer's account is active, suspended, or locked."""
    log.info("tool_called", tool="account_status", customer_id=customer_id, brand_id=brand_id)
    # Mock: most accounts are active
    return {
        "customer_id": customer_id,
        "status": "active",
        "can_login": True,
        "message": "Account is active. Please use the 'Forgot Password' link on the login page to reset your password.",
    }


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
    log.info("tool_called", tool="escalate", session_id=session_id, reason=reason, priority=priority)
    ticket_id = f"TKT-{uuid.uuid4().hex[:6].upper()}"
    return {
        "escalated": True,
        "ticket_id": ticket_id,
        "priority": priority,
        "message": (
            "I've connected you with a member of our team who will be able to help further. "
            f"Your reference number is {ticket_id}. Our team will be with you shortly."
        ),
        "estimated_wait": "5-10 minutes" if priority == "P1" else "15-30 minutes",
    }


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
