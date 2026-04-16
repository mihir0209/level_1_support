"""
Tests for the Context Engineering Engine.
Uses mocked LLM and graph clients — no external services required.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from core.context.engine import (
    compress_session,
    _format_policy_block,
    _format_resolution_block,
    _format_user_context_block,
)
from core.ontology.models import Channel, Customer, CustomerTier, Order, OrderStatus


# ─────────────────────────────────────────────────────────────────────────
# Session compression tests
# ─────────────────────────────────────────────────────────────────────────

def test_compress_session_empty():
    result = compress_session([])
    assert "No prior" in result


def test_compress_session_short():
    history = [
        {"role": "user", "content": "Where is my order?"},
        {"role": "assistant", "content": "Let me check that for you."},
    ]
    result = compress_session(history)
    assert "Where is my order?" in result
    assert "Let me check" in result


def test_compress_session_long():
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}"}
        for i in range(20)
    ]
    result = compress_session(history, max_recent_turns=4)
    # Should contain recent messages
    assert "Message 18" in result or "Message 19" in result
    # Should have a summary of earlier context
    assert "Earlier" in result or "Earlier context" in result


# ─────────────────────────────────────────────────────────────────────────
# Policy block formatting tests
# ─────────────────────────────────────────────────────────────────────────

def test_format_policy_block_full():
    policy = {
        "return_window_days": 28,
        "return_cost_gbp": 1.99,
        "return_cost_usd": 0.0,
        "free_return_condition": None,
        "excluded_categories": ["swimwear", "lingerie"],
    }
    result = _format_policy_block(policy, "Boohoo")
    assert "28 days" in result
    assert "£1.99" in result
    assert "swimwear" in result
    assert "21 days" in result   # refund timeline


def test_format_policy_block_premium_free_returns():
    policy = {
        "return_window_days": 28,
        "return_cost_gbp": 0.0,
        "return_cost_usd": 0.0,
        "free_return_condition": "All orders eligible",
        "excluded_categories": [],
    }
    result = _format_policy_block(policy, "Karen Millen")
    assert "All orders eligible" in result


def test_format_policy_block_none():
    result = _format_policy_block(None, "TestBrand")
    assert "TestBrand" in result
    assert "return policy" in result.lower()


# ─────────────────────────────────────────────────────────────────────────
# Resolution block formatting tests
# ─────────────────────────────────────────────────────────────────────────

def test_format_resolution_block_empty():
    result = _format_resolution_block([])
    assert "No specific" in result


def test_format_resolution_block_with_steps():
    resolutions = [{
        "id": "wismo_track",
        "name": "Provide tracking information",
        "steps": [
            {"order": 1, "instruction": "Look up order status.", "tool_required": "order_lookup", "condition": None},
            {"order": 2, "instruction": "Provide tracking URL.", "tool_required": "shipment_track", "condition": None},
        ],
        "success_rate": 0.92,
        "escalation_trigger": None,
    }]
    result = _format_resolution_block(resolutions)
    assert "tracking information" in result.lower()
    assert "order_lookup" in result
    assert "92%" in result


# ─────────────────────────────────────────────────────────────────────────
# User context block formatting tests
# ─────────────────────────────────────────────────────────────────────────

def test_format_user_context_block_none():
    result = _format_user_context_block(None)
    assert "guest" in result.lower() or "not identified" in result.lower()


def test_format_user_context_block_with_order():
    customer = Customer(
        id="cust_001",
        brand_id="boohoo_uk",
        tier=CustomerTier.LOYALTY,
        locale="en-GB",
        total_orders=10,
        recent_orders=[
            Order(
                id="BH-12345",
                brand_id="boohoo_uk",
                customer_id="cust_001",
                status=OrderStatus.IN_TRANSIT,
                placed_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
                total_value=45.99,
                currency="GBP",
            )
        ],
    )
    result = _format_user_context_block(customer)
    assert "cust_001" in result
    assert "BH-12345" in result
    assert "loyalty" in result.lower()
    assert "10" in result


# ─────────────────────────────────────────────────────────────────────────
# Intent classification tests (mocked LLM)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_intent_wismo():
    from core.context.engine import classify_intent

    mock_llm = AsyncMock()
    mock_llm.generate = AsyncMock(return_value="""
    {
        "issue_type": "wismo",
        "confidence": 0.95,
        "category": "order",
        "entities": {"order_id": "BH-12345", "product_sku": null, "return_reason": null, "promo_code": null, "payment_method": null, "item_description": null}
    }
    """)

    result = await classify_intent(
        message="Where is my order BH-12345?",
        brand_id="boohoo_uk",
        conversation_history=[],
        llm_client=mock_llm,
    )

    assert result.issue_type == "wismo"
    assert result.confidence >= 0.9
    assert result.entities.get("order_id") == "BH-12345"


@pytest.mark.asyncio
async def test_classify_intent_return():
    from core.context.engine import classify_intent

    mock_llm = AsyncMock()
    mock_llm.generate = AsyncMock(return_value="""
    {
        "issue_type": "return_initiate",
        "confidence": 0.88,
        "category": "return",
        "entities": {"order_id": null, "product_sku": null, "return_reason": "wrong size", "promo_code": null, "payment_method": null, "item_description": "blue dress"}
    }
    """)

    result = await classify_intent(
        message="I want to return my blue dress, it's the wrong size",
        brand_id="plt_uk",
        conversation_history=[],
        llm_client=mock_llm,
    )

    assert result.issue_type == "return_initiate"
    assert "wrong size" in str(result.entities)
