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
    assert "no order found" in result["message"].lower() or "not found" in result["message"].lower()


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


def test_dynamic_tool_schema_generation():
    """Verify that dynamic tools have correct args_schema generated based on their specs."""
    from core.tools.api_registry import APIToolSpec
    from core.tools.dynamic_builder import _build_args_schema

    spec = APIToolSpec(
        name="test_endpoint",
        description="A test dynamic endpoint",
        method="POST",
        base_url="https://api.example.com",
        path="/api/v1/test",
        query_params=[
            {"name": "param_q", "type": "integer", "required": True, "description": "A query param"}
        ],
        body_schema={
            "type": "object",
            "required": ["param_b"],
            "properties": {
                "param_b": {"type": "boolean", "description": "A body param"},
                "param_opt": {"type": "string", "description": "An optional body param"}
            }
        },
        requires_auth=False,
        tags=["test"],
    )

    args_schema = _build_args_schema(spec, "test_endpoint")
    assert args_schema is not None

    # Assert model fields and types
    fields = args_schema.model_fields
    assert "param_q" in fields
    assert "param_b" in fields
    assert "param_opt" in fields

    # Check types
    assert fields["param_q"].annotation == int
    assert fields["param_b"].annotation == bool
    assert fields["param_opt"].annotation == str

    # Check required fields
    assert fields["param_q"].is_required() is True
    assert fields["param_b"].is_required() is True
    assert fields["param_opt"].is_required() is False


def test_dynamic_sql_tool_execution(monkeypatch):
    """Verify that a dynamic tool with a SQL connector type executes queries on the DB pool."""
    from core.tools.api_registry import APIToolSpec
    from core.tools.dynamic_builder import _build_tool_func
    import json
    from unittest.mock import MagicMock

    spec = APIToolSpec(
        name="test_sql_tool",
        description="A test SQL tool",
        method="GET",
        base_url="https://api.example.com",
        path="",
        query_params=[],
        body_schema=None,
        requires_auth=False,
        tags=["test"],
        connector_type="sql",
        connector_config={
            "query": "SELECT * FROM test_table WHERE id = :id"
        }
    )

    mock_execute = MagicMock()
    mock_fetchall = MagicMock(return_value=[{"id": 42, "name": "Test Row"}])
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.execute = mock_execute
    mock_cursor.fetchall = mock_fetchall
    mock_cursor.description = [("id",), ("name",)]

    class FakeConnection:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
        def cursor(self, row_factory=None):
            return mock_cursor

    class FakePool:
        def connection(self):
            return FakeConnection()

    monkeypatch.setattr("core.tools.dynamic_builder._get_pool", lambda: FakePool())

    func = _build_tool_func(spec)
    result = func(id=42)

    # Assert cursor executed the formatted query with correct params
    mock_execute.assert_called_once_with("SELECT * FROM test_table WHERE id = %(id)s", {"id": 42})
    assert "Test Row" in result


def test_dynamic_python_tool_module_execution(monkeypatch):
    """Verify dynamic python tool execution using module/function."""
    from core.tools.api_registry import APIToolSpec
    from core.tools.dynamic_builder import _build_tool_func
    import sys
    from types import ModuleType

    # Create dummy module and add to sys.modules
    dummy = ModuleType("dummy_module")
    dummy.my_func = lambda val: {"result": val * 2}
    sys.modules["dummy_module"] = dummy

    spec = APIToolSpec(
        name="test_py_tool",
        description="A test Python tool",
        method="GET",
        base_url="https://api.example.com",
        path="",
        query_params=[],
        body_schema=None,
        requires_auth=False,
        tags=["test"],
        connector_type="python",
        connector_config={
            "module": "dummy_module",
            "function": "my_func"
        }
    )

    func = _build_tool_func(spec)
    result = func(val=10)
    assert "20" in result



def test_dynamic_python_tool_inline_execution():
    """Verify dynamic python tool execution using inline code."""
    from core.tools.api_registry import APIToolSpec
    from core.tools.dynamic_builder import _build_tool_func

    spec = APIToolSpec(
        name="test_py_inline_tool",
        description="A test Python inline tool",
        method="GET",
        base_url="https://api.example.com",
        path="",
        query_params=[],
        body_schema=None,
        requires_auth=False,
        tags=["test"],
        connector_type="python",
        connector_config={
            "code": "result = params.get('val') * 2"
        }
    )

    func = _build_tool_func(spec)
    result = func(val=21)
    assert "42" in result

