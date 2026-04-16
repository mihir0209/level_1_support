"""
Tests for the tool registry — all Phase 1 mock implementations.
"""

from __future__ import annotations

from core.tools.registry import (
    order_lookup,
    shipment_track,
    return_init,
    refund_status,
    cancel_order,
    promo_validate,
    size_guide,
    account_status,
)


def test_order_lookup_found():
    result = order_lookup.invoke({"order_id": "BH-12345-UK", "brand_id": "boohoo_uk"})
    assert result["found"] is True
    assert result["id"] == "BH-12345-UK"
    assert result["status"] in ["in_transit", "processing", "delivered", "dispatched"]


def test_order_lookup_not_found():
    result = order_lookup.invoke({"order_id": "NONEXISTENT-999", "brand_id": "boohoo_uk"})
    assert result["found"] is False
    assert "not found" in result["message"].lower()


def test_order_lookup_wrong_brand():
    result = order_lookup.invoke({"order_id": "BH-12345-UK", "brand_id": "plt_uk"})
    assert result["found"] is False


def test_shipment_track_found():
    result = shipment_track.invoke({"order_id": "BH-12345-UK", "brand_id": "boohoo_uk"})
    assert result["tracked"] is True
    assert "tracking_id" in result


def test_shipment_track_not_dispatched():
    result = shipment_track.invoke({"order_id": "PLT-98765-UK", "brand_id": "plt_uk"})
    assert result["tracked"] is False


def test_return_init_success():
    result = return_init.invoke({
        "order_id": "BH-12345-UK",
        "brand_id": "boohoo_uk",
        "items_to_return": ["BH-DRESS-001"],
        "return_reason": "wrong_size",
    })
    assert result["success"] is True
    assert result["return_ref"].startswith("RET-")
    assert result["refund_eta_days"] == 21


def test_cancel_order_processing():
    # PLT-98765-UK is in 'processing' status
    result = cancel_order.invoke({"order_id": "PLT-98765-UK", "brand_id": "plt_uk"})
    assert result["success"] is True
    assert "cancelled" in result["message"].lower()


def test_cancel_order_dispatched():
    result = cancel_order.invoke({"order_id": "BH-12345-UK", "brand_id": "boohoo_uk"})
    assert result["success"] is False
    assert "cannot be cancelled" in result["reason"].lower()


def test_promo_validate_valid():
    result = promo_validate.invoke({
        "promo_code": "SAVE10",
        "brand_id": "boohoo_uk",
        "basket_value": 50.0,
    })
    assert result["valid"] is True
    assert result["discount_pct"] == 10


def test_promo_validate_min_spend_not_met():
    result = promo_validate.invoke({
        "promo_code": "SAVE10",
        "brand_id": "boohoo_uk",
        "basket_value": 10.0,
    })
    assert result["valid"] is False
    assert "minimum spend" in result["reason"].lower()


def test_promo_validate_expired():
    result = promo_validate.invoke({
        "promo_code": "EXPIRED5",
        "brand_id": "boohoo_uk",
        "basket_value": 50.0,
    })
    assert result["valid"] is False
    assert "expired" in result["reason"].lower()


def test_promo_validate_wrong_brand():
    result = promo_validate.invoke({
        "promo_code": "SAVE10",
        "brand_id": "karenmillen_uk",
        "basket_value": 50.0,
    })
    assert result["valid"] is False


def test_size_guide_returns_data():
    result = size_guide.invoke({
        "product_category": "dresses",
        "brand_id": "boohoo_uk",
    })
    assert "size_guide" in result
    assert "M" in result["size_guide"]
    assert "UK" in result["size_guide"]["M"]


def test_account_status_active():
    result = account_status.invoke({
        "customer_id": "cust_001",
        "brand_id": "boohoo_uk",
    })
    assert result["status"] == "active"
    assert result["can_login"] is True


def test_refund_status_not_found():
    result = refund_status.invoke({
        "return_ref": "RET-NONEXISTENT",
        "order_id": None,
    })
    assert result["found"] is False
