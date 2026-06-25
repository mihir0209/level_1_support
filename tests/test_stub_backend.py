import os
from pathlib import Path
import pytest

from core.tools.backends import set_tool_backend, get_tool_backend
from core.tools.backends.stub_backend import StubBackend
from core.tools.registry import (
    order_lookup,
    shipment_track,
    promo_validate,
    faq_search,
)

@pytest.fixture(autouse=True)
def setup_stub_backend():
    # Set the active backend to StubBackend pointing to the root fixtures directory
    root_dir = Path(__file__).parent.parent
    stub = StubBackend(fixtures_dir=root_dir / "fixtures")
    set_tool_backend(stub)
    yield
    # Reset to default
    set_tool_backend(None)


def test_stub_order_lookup_success():
    res = order_lookup.invoke({"order_id": "BH-12345-UK", "brand_id": "boohoo_uk"})
    assert res["found"] is True
    assert res["id"] == "BH-12345-UK"
    assert res["status"] == "in_transit"
    assert len(res["items"]) == 1
    assert res["items"][0]["sku"] == "BH-DRESS-001"


def test_stub_order_lookup_cancelled():
    res = order_lookup.invoke({"order_id": "BH-77777-UK", "brand_id": "boohoo_uk"})
    assert res["found"] is True
    assert res["status"] == "cancelled"


def test_stub_order_lookup_not_found():
    res = order_lookup.invoke({"order_id": "NONEXISTENT-999", "brand_id": "boohoo_uk"})
    assert res["found"] is False
    assert "not found" in res["message"].lower() or "no order" in res["message"].lower()


def test_stub_order_lookup_wrong_brand():
    res = order_lookup.invoke({"order_id": "BH-12345-UK", "brand_id": "plt_uk"})
    assert res["found"] is False
    assert "not found for this brand" in res["message"].lower()


def test_stub_shipment_track_success():
    res = shipment_track.invoke({"order_id": "BH-12345-UK", "brand_id": "boohoo_uk"})
    assert res["tracked"] is True
    assert res["carrier"] == "Evri"
    assert res["tracking_id"] == "EVRI-987654321"
    assert res["current_status"] == "Out for delivery"


def test_stub_promo_validate_valid():
    res = promo_validate.invoke({
        "promo_code": "SAVE10",
        "brand_id": "boohoo_uk",
        "basket_value": 50.0,
    })
    assert res["valid"] is True
    assert res["discount_pct"] == 10
    assert "10%" in res["message"]


def test_stub_promo_validate_min_spend_failed():
    res = promo_validate.invoke({
        "promo_code": "SAVE10",
        "brand_id": "boohoo_uk",
        "basket_value": 15.0,
    })
    assert res["valid"] is False
    assert "minimum spend" in res["reason"].lower()


def test_stub_promo_validate_expired():
    res = promo_validate.invoke({
        "promo_code": "EXPIRED5",
        "brand_id": "boohoo_uk",
        "basket_value": 20.0,
    })
    assert res["valid"] is False
    assert "expired" in res["reason"].lower()


def test_stub_faq_search_success():
    res = faq_search.invoke({"query": "return item", "brand_id": "boohoo_uk"})
    assert res["found"] is True
    assert "answer" in res
    assert "Evri parcel point" in res["answer"]
