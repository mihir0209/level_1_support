"""Demo administrative tools — real order, customer, and return management APIs.

These expose the mock data layer over REST with API key authentication.
Configured via ADMIN_API_KEY env var. Authorize with X-API-KEY header.
Swagger UI at: http://localhost:8000/docs → Demo Tools section
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel

from brands.registry import BRAND_REGISTRY, get_brand_by_id
from core.tools.registry import (
    _MOCK_ORDERS,
    _MOCK_RETURNS,
    _MOCK_PROMOS,
    TOOL_REGISTRY,
)
from config import settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Operations"])

# ── Auth dependency ───────────────────────────────────────────────────────────
def verify_api_key(x_api_key: str = Header(None, description="API key. Default: `l1support-demo-key-2026` (override via ADMIN_API_KEY env var)")) -> str:
    expected = settings.admin_api_key
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail=f"Missing X-API-KEY header. Default demo value: l1support-demo-key-2026",
        )
    if x_api_key != expected:
        raise HTTPException(status_code=403, detail=f"Invalid API key. Default demo value: l1support-demo-key-2026")
    return x_api_key


# ── Request/Response models ────────────────────────────────────────────────────
class OrderResponse(BaseModel):
    id: str
    brand_id: str
    customer_id: str
    status: str
    total_value: float
    currency: str
    items: list[dict]
    shipment: dict | None
    placed_at: str


class CustomerListResponse(BaseModel):
    total: int
    customers: list[dict]


class ReturnCreateRequest(BaseModel):
    order_id: str
    brand_id: str
    items: list[str]
    reason: str = "not_specified"


class ReturnResponse(BaseModel):
    success: bool
    return_ref: str
    message: str
    refund_eta_days: int


# ══════════════════════════════════════════════════════════════════════════════
# BRANDS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/brands", tags=["Brands"])
def list_brands_api(_auth: str = Depends(verify_api_key)):
    """List all registered brands with brand_ids — use these as brand_id in other endpoints."""
    return {
        "total": len(BRAND_REGISTRY),
        "brands": [
            {
                "brand_id": b.brand_id,
                "name": b.name,
                "domains": b.domains,
                "locales": b.locales,
                "default_region": b.default_region,
            }
            for b in BRAND_REGISTRY
        ],
    }


@router.get("/brands/{brand_id}", tags=["Brands"])
def get_brand_api(brand_id: str, _auth: str = Depends(verify_api_key)):
    """Full detail for a single brand by brand_id."""
    entry = get_brand_by_id(brand_id)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"Brand '{brand_id}' not found. Call GET /api/v1/brands to see valid brand_ids.",
        )
    return {
        "brand_id": entry.brand_id,
        "name": entry.name,
        "domains": entry.domains,
        "locales": entry.locales,
        "default_region": entry.default_region,
        "chatwoot_inbox_id": entry.chatwoot_inbox_id,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMERS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/customers", response_model=CustomerListResponse)
def list_customers(
    brand_id: str | None = Query(
        default=None,
        description="Filter by brand_id. Call `GET /api/v1/brands` to see all valid brand_ids (e.g. `boohoo_uk`, `plt_uk`, `karenmillen_uk`).",
        example="boohoo_uk",
    ),
    limit: int = Query(default=50, le=200),
    _auth: str = Depends(verify_api_key),
):
    """
    List customers derived from order history.

    **Available demo brand_ids:** `boohoo_uk` · `plt_uk` · `karenmillen_uk`
    (call `GET /api/v1/brands` for the full list)

    **Known demo customer_ids:** `cust_001` (boohoo_uk) · `cust_002` (plt_uk) · `cust_003` (karenmillen_uk)
    """
    seen: dict[str, dict] = {}
    for order in _MOCK_ORDERS.values():
        if brand_id and order["brand_id"] != brand_id:
            continue
        cid = order["customer_id"]
        if cid not in seen:
            seen[cid] = {
                "customer_id": cid,
                "brand_id": order["brand_id"],
                "order_count": 0,
                "total_spend": 0.0,
            }
        seen[cid]["order_count"] += 1
        seen[cid]["total_spend"] += order["total_value"]

    customers = list(seen.values())[:limit]
    return CustomerListResponse(total=len(customers), customers=customers)


@router.get("/customers/{customer_id}")
def get_customer(
    customer_id: str = Path(
        description="Customer ID. Demo values: `cust_001` (boohoo_uk), `cust_002` (plt_uk), `cust_003` (karenmillen_uk).",
        example="cust_001",
    ),
    _auth: str = Depends(verify_api_key),
):
    """Full customer profile — all orders and returns. Demo IDs: `cust_001`, `cust_002`, `cust_003`."""
    orders = [o for o in _MOCK_ORDERS.values() if o["customer_id"] == customer_id]
    returns = [r for r in _MOCK_RETURNS.values() if r["customer_id"] == customer_id]
    if not orders:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {
        "customer_id": customer_id,
        "total_orders": len(orders),
        "total_spend": sum(o["total_value"] for o in orders),
        "orders": orders,
        "returns": returns,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ORDERS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/orders", response_model=dict)
def list_orders(
    brand_id: str | None = None,
    customer_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
    _auth: str = Depends(verify_api_key),
):
    """List orders with optional filters."""
    orders = list(_MOCK_ORDERS.values())
    if brand_id:
        orders = [o for o in orders if o["brand_id"] == brand_id]
    if customer_id:
        orders = [o for o in orders if o["customer_id"] == customer_id]
    if status:
        orders = [o for o in orders if o["status"] == status]
    return {"total": len(orders), "orders": orders[:limit]}


@router.get("/orders/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: str,
    _auth: str = Depends(verify_api_key),
):
    """Single order detail."""
    order = _MOCK_ORDERS.get(order_id.upper())
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderResponse(**order)


@router.post("/orders/{order_id}/cancel")
def cancel_order_api(
    order_id: str,
    _auth: str = Depends(verify_api_key),
):
    """Cancel an order (only while processing)."""
    order = _MOCK_ORDERS.get(order_id.upper())
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] != "processing":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel — order is {order['status']}",
        )
    order["status"] = "cancelled"
    return {"success": True, "message": "Order cancelled. Refund within 3-5 days."}


# ══════════════════════════════════════════════════════════════════════════════
# RETURNS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/returns", response_model=dict)
def list_returns(
    brand_id: str | None = None,
    status: str | None = None,
    _auth: str = Depends(verify_api_key),
):
    """List all initiated returns."""
    returns = list(_MOCK_RETURNS.values())
    if brand_id:
        returns = [r for r in returns if r["brand_id"] == brand_id]
    if status:
        returns = [r for r in returns if r["status"] == status]
    return {"total": len(returns), "returns": returns}


@router.get("/returns/{return_ref}")
def get_return(
    return_ref: str,
    _auth: str = Depends(verify_api_key),
):
    """Return detail by reference."""
    ret = _MOCK_RETURNS.get(return_ref.upper())
    if not ret:
        raise HTTPException(status_code=404, detail="Return not found")
    return ret


@router.post("/returns", response_model=ReturnResponse)
def create_return(
    req: ReturnCreateRequest,
    _auth: str = Depends(verify_api_key),
):
    """Initiate a new return for an order."""
    order = _MOCK_ORDERS.get(req.order_id.upper())
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return_ref = f"RET-{uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc)
    _MOCK_RETURNS[return_ref] = {
        "return_ref": return_ref,
        "order_id": req.order_id.upper(),
        "brand_id": req.brand_id,
        "items": req.items,
        "reason": req.reason,
        "status": "initiated",
        "initiated_at": now.isoformat(),
        "customer_id": order["customer_id"],
        "refund_eta": (now.replace(day=now.day + 21)).isoformat(),
    }
    log.info("return_created_api", return_ref=return_ref, order_id=req.order_id)
    return ReturnResponse(
        success=True,
        return_ref=return_ref,
        message=f"Return initiated for order {req.order_id}. Reference: {return_ref}",
        refund_eta_days=21,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PROMOS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/promos")
def list_promos(
    active_only: bool = False,
    _auth: str = Depends(verify_api_key),
):
    """List promotional codes."""
    promos = list(_MOCK_PROMOS.values())
    if active_only:
        promos = [p for p in promos if p["active"]]
    return {"total": len(promos), "promos": promos}


@router.post("/promos/validate")
def validate_promo(
    code: str = Query(...),
    brand_id: str = Query(...),
    basket_value: float = Query(default=0.0),
    _auth: str = Depends(verify_api_key),
):
    """Validate a promo code for a brand and basket."""
    fn = TOOL_REGISTRY["promo_validate"]
    result = fn.invoke({"promo_code": code, "brand_id": brand_id, "basket_value": basket_value})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# AGENT TOOLS (raw)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/tools")
def list_tools(_auth: str = Depends(verify_api_key)):
    """List all available agent tools and their parameters."""
    import inspect
    tools = []
    for name, fn in TOOL_REGISTRY.items():
        sig = inspect.signature(fn.invoke)
        tools.append({
            "name": name,
            "description": getattr(fn, "description", "") or "",
            "parameters": [
                {"name": p, "description": ""}
                for p in sig.parameters
                if p not in ("name", "kwargs")
            ],
        })
    return {"total": len(tools), "tools": tools}


@router.post("/tools/{tool_name}/call")
def call_tool(
    tool_name: str,
    params: dict[str, Any],
    _auth: str = Depends(verify_api_key),
):
    """Call an agent tool directly with parameters."""
    if tool_name not in TOOL_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_name}")
    fn = TOOL_REGISTRY[tool_name]
    try:
        result = fn.invoke(params)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# API KEY MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/auth/key")
def get_current_key(_auth: str = Depends(verify_api_key)):
    """Get the current admin API key for this instance."""
    admin_env = getattr(settings, "admin_api_key", None) or ""
    return {
        "api_key": admin_env if admin_env else _get_admin_key(),
        "hint": "Set ADMIN_API_KEY env var to persist across restarts",
    }
