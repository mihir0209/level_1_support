"""
Demo Ecommerce API — Flask app with SQLite backend.
Routes: /<brand_id>/api/v1/<endpoint>

This is a realistic e-commerce API that mirrors what a real brand's backend
would expose. The L1 support system will register these endpoints as tools.

Usage:
  python app.py [--port 8001] [--db ecommerce.db]
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, abort
from flask_cors import CORS

from models import (
    Base, Customer, Product, Order, OrderItem, Shipment, Return, ReturnItem, Promo,
    make_engine, make_session,
)


def make_app(db_path: str = "ecommerce.db") -> Flask:
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    session_factory = lambda: make_session(engine)

    app = Flask(__name__)
    CORS(app, resources={r"/<brand_id>/*": {"origins": "*"}})

    def get_session():
        return make_session(engine)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def json_response(data, status: int = 200):
        return jsonify(data), status

    def not_found(msg: str = "Not found"):
        return json_response({"error": True, "message": msg}, 404)

    def bad_request(msg: str):
        return json_response({"error": True, "message": msg}, 400)

    def ok(data: dict):
        return json_response(data)

    # ── Customer endpoints ──────────────────────────────────────────────────

    @app.route("/<brand_id>/api/v1/customers", methods=["GET"])
    def list_customers(brand_id):
        """List customers for a brand, optionally search by email or name."""
        session = get_session()
        try:
            query = session.query(Customer).filter(Customer.brand_id == brand_id)
            email = request.args.get("email")
            name = request.args.get("name")
            if email:
                query = query.filter(Customer.email.ilike(f"%{email}%"))
            if name:
                query = query.filter(Customer.name.ilike(f"%{name}%"))
            limit = min(int(request.args.get("limit", 10)), 50)
            customers = query.limit(limit).all()
            return ok({
                "brand_id": brand_id,
                "count": len(customers),
                "customers": [c.to_dict() for c in customers],
            })
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/customers/<customer_id>", methods=["GET"])
    def get_customer(brand_id, customer_id):
        session = get_session()
        try:
            c = session.query(Customer).filter_by(id=customer_id, brand_id=brand_id).first()
            if not c:
                return not_found(f"Customer {customer_id} not found")
            detailed = request.args.get("include_orders", "false") == "true"
            return ok(c.to_dict(include_orders=detailed))
        finally:
            session.close()

    # ── Order endpoints ────────────────────────────────────────────────────

    @app.route("/<brand_id>/api/v1/orders", methods=["GET"])
    def list_orders(brand_id):
        """List orders. Filter by customer_id, status, or date range."""
        session = get_session()
        try:
            query = session.query(Order).filter(Order.brand_id == brand_id)
            customer_id = request.args.get("customer_id")
            status = request.args.get("status")
            if customer_id:
                query = query.filter(Order.customer_id == customer_id)
            if status:
                query = query.filter(Order.status == status)
            limit = min(int(request.args.get("limit", 20)), 100)
            orders = query.order_by(Order.placed_at.desc()).limit(limit).all()
            return ok({
                "brand_id": brand_id,
                "count": len(orders),
                "orders": [o.to_dict(detailed=True) for o in orders],
            })
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/orders/<order_id>", methods=["GET"])
    def get_order(brand_id, order_id):
        session = get_session()
        try:
            order = session.query(Order).filter_by(id=order_id, brand_id=brand_id).first()
            if not order:
                return not_found(f"Order {order_id} not found")
            return ok(order.to_dict(detailed=True))
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/orders/<order_id>/status", methods=["GET"])
    def get_order_status(brand_id, order_id):
        """Quick status check — minimal payload."""
        session = get_session()
        try:
            order = session.query(Order).filter_by(id=order_id, brand_id=brand_id).first()
            if not order:
                return not_found(f"Order {order_id} not found")
            result = {
                "order_id": order.id,
                "status": order.status,
                "placed_at": order.placed_at.isoformat() if order.placed_at else None,
                "shipment_status": order.shipment.status if order.shipment else None,
                "estimated_delivery": (
                    order.shipment.estimated_delivery.isoformat()
                    if order.shipment and order.shipment.estimated_delivery else None
                ),
            }
            return ok(result)
        finally:
            session.close()

    # ── Return endpoints ───────────────────────────────────────────────────

    @app.route("/<brand_id>/api/v1/returns", methods=["GET"])
    def list_returns(brand_id):
        session = get_session()
        try:
            query = session.query(Return).filter(Return.brand_id == brand_id)
            customer_id = request.args.get("customer_id")
            order_id = request.args.get("order_id")
            status = request.args.get("status")
            if customer_id:
                query = query.filter(Return.customer_id == customer_id)
            if order_id:
                query = query.filter(Return.order_id == order_id)
            if status:
                query = query.filter(Return.status == status)
            limit = min(int(request.args.get("limit", 20)), 100)
            returns = query.order_by(Return.initiated_at.desc()).limit(limit).all()
            return ok({
                "brand_id": brand_id,
                "count": len(returns),
                "returns": [r.to_dict(detailed=True) for r in returns],
            })
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/returns/<return_id>", methods=["GET"])
    def get_return(brand_id, return_id):
        session = get_session()
        try:
            ret = session.query(Return).filter_by(id=return_id, brand_id=brand_id).first()
            if not ret:
                return not_found(f"Return {return_id} not found")
            return ok(ret.to_dict(detailed=True))
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/returns/<return_id>/refund-status", methods=["GET"])
    def get_refund_status(brand_id, return_id):
        """Quick refund status — what the agent needs."""
        session = get_session()
        try:
            ret = session.query(Return).filter_by(id=return_id, brand_id=brand_id).first()
            if not ret:
                return not_found(f"Return {return_id} not found")
            initiated = ret.initiated_at
            if initiated and not initiated.tzinfo:
                initiated = initiated.replace(tzinfo=timezone.utc)
            now_aware = datetime.now(timezone.utc)
            if not initiated:
                initiated = now_aware
            days_elapsed = (now_aware - initiated).days
            refund_due = ret.refund_expected_by
            if refund_due and not refund_due.tzinfo:
                refund_due = refund_due.replace(tzinfo=timezone.utc)
            if not refund_due:
                refund_due = initiated + timedelta(days=14)
            days_remaining = max(0, (refund_due - datetime.now(timezone.utc)).days)
            return ok({
                "return_id": ret.id,
                "order_id": ret.order_id,
                "status": ret.status,
                "reason": ret.reason,
                "initiated_at": initiated.isoformat(),
                "refund_amount": ret.refund_amount,
                "refund_method": ret.refund_method,
                "days_elapsed": days_elapsed,
                "days_remaining": days_remaining,
                "refund_expected_by": refund_due.isoformat(),
                "refund_sent_at": ret.refund_sent_at.isoformat() if ret.refund_sent_at else None,
                "overdue": ret.status not in ("completed", "refund_sent") and days_elapsed > 14,
            })
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/returns", methods=["POST"])
    def initiate_return(brand_id):
        """Initiate a return for an order."""
        session = get_session()
        try:
            data = request.get_json() or {}
            order_id = data.get("order_id")
            reason = data.get("reason", "changed_mind")
            items = data.get("items", [])
            customer_note = data.get("note", "")

            if not order_id:
                return bad_request("order_id is required")

            order = session.query(Order).filter_by(id=order_id, brand_id=brand_id).first()
            if not order:
                return not_found(f"Order {order_id} not found")

            if order.status not in ("delivered", "in_transit"):
                return bad_request(f"Cannot return order with status: {order.status}")

            return_id = f"RET-{uuid.uuid4().hex[:8].upper()}"
            refund_amount = sum(i.price_paid * i.quantity for i in order.items)
            refund_expected = datetime.now(timezone.utc) + timedelta(days=14)

            ret = Return(
                id=return_id,
                brand_id=brand_id,
                order_id=order_id,
                customer_id=order.customer_id,
                status="initiated",
                reason=reason,
                customer_note=customer_note,
                refund_amount=refund_amount,
                refund_expected_by=refund_expected,
            )
            session.add(ret)

            order.status = "return_pending"
            session.commit()

            return ok({
                "success": True,
                "return_id": return_id,
                "message": f"Return initiated. Reference: {return_id}. Label will be emailed.",
                "refund_amount": refund_amount,
                "refund_expected_by": refund_expected.isoformat(),
                "status": "initiated",
            }), 201
        finally:
            session.close()

    # ── Shipment endpoints ──────────────────────────────────────────────────

    @app.route("/<brand_id>/api/v1/shipments/track", methods=["GET"])
    def track_shipment(brand_id):
        """Track a shipment by order ID or tracking number."""
        session = get_session()
        try:
            order_id = request.args.get("order_id")
            tracking_id = request.args.get("tracking_id")

            if not order_id and not tracking_id:
                return bad_request("order_id or tracking_id is required")

            shipment = None
            if order_id:
                order = session.query(Order).filter_by(id=order_id, brand_id=brand_id).first()
                shipment = order.shipment if order else None
            else:
                shipment = session.query(Shipment).filter_by(tracking_id=tracking_id).first()

            if not shipment:
                return not_found("Shipment not found")

            return ok(shipment.to_dict())
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/shipments/<shipment_id>", methods=["GET"])
    def get_shipment(brand_id, shipment_id):
        session = get_session()
        try:
            s = session.query(Shipment).filter_by(id=shipment_id).first()
            if not s:
                return not_found(f"Shipment {shipment_id} not found")
            return ok(s.to_dict())
        finally:
            session.close()

    # ── Product endpoints ───────────────────────────────────────────────────

    @app.route("/<brand_id>/api/v1/products", methods=["GET"])
    def list_products(brand_id):
        session = get_session()
        try:
            query = session.query(Product).filter(Product.brand_id == brand_id)
            category = request.args.get("category")
            sku = request.args.get("sku")
            if category:
                query = query.filter(Product.category.ilike(f"%{category}%"))
            if sku:
                query = query.filter(Product.sku == sku)
            limit = min(int(request.args.get("limit", 50)), 200)
            products = query.limit(limit).all()
            return ok({
                "brand_id": brand_id,
                "count": len(products),
                "products": [p.to_dict() for p in products],
            })
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/products/<sku>", methods=["GET"])
    def get_product(brand_id, sku):
        session = get_session()
        try:
            p = session.query(Product).filter_by(sku=sku, brand_id=brand_id).first()
            if not p:
                return not_found(f"Product {sku} not found")
            return ok(p.to_dict())
        finally:
            session.close()

    # ── Promo endpoints ───────────────────────────────────────────────────

    @app.route("/<brand_id>/api/v1/promos/validate", methods=["POST"])
    def validate_promo(brand_id):
        """Validate a promo code."""
        session = get_session()
        try:
            data = request.get_json() or {}
            code = data.get("code", "").strip().upper()
            basket_value = float(data.get("basket_value", 0.0))

            promo = session.query(Promo).filter_by(code=code, brand_id=brand_id).first()
            if not promo:
                return ok({"valid": False, "reason": "Code not found for this brand"})

            if not promo.active:
                return ok({"valid": False, "reason": "This code has expired or is no longer active"})

            if promo.expires_at and promo.expires_at < datetime.now().replace(tzinfo=promo.expires_at.tzinfo):
                return ok({"valid": False, "reason": "This code has expired"})

            if promo.usage_limit and promo.usage_count >= promo.usage_limit:
                return ok({"valid": False, "reason": "This code has reached its usage limit"})

            if basket_value < promo.min_spend:
                return ok({
                    "valid": False,
                    "reason": f"Minimum spend of £{promo.min_spend:.2f} required",
                })

            discount = promo.discount_value
            if promo.discount_type == "percent":
                discount_amount = min(basket_value * (promo.discount_value / 100), promo.max_discount or float("inf"))
            else:
                discount_amount = promo.discount_value

            return ok({
                "valid": True,
                "code": promo.code,
                "discount_type": promo.discount_type,
                "discount_value": promo.discount_value,
                "discount_amount": round(discount_amount, 2),
                "min_spend": promo.min_spend,
                "message": f"Code applied! You save £{discount_amount:.2f}",
            })
        finally:
            session.close()

    @app.route("/<brand_id>/api/v1/promos", methods=["GET"])
    def list_promos(brand_id):
        session = get_session()
        try:
            active_only = request.args.get("active", "false") == "true"
            query = session.query(Promo).filter(Promo.brand_id == brand_id)
            if active_only:
                query = query.filter(Promo.active == True)
            promos = query.all()
            return ok({
                "brand_id": brand_id,
                "count": len(promos),
                "promos": [p.to_dict() for p in promos],
            })
        finally:
            session.close()

    # ── Account endpoints ─────────────────────────────────────────────────

    @app.route("/<brand_id>/api/v1/accounts/status", methods=["GET"])
    def account_status(brand_id):
        """Check account status."""
        session = get_session()
        try:
            customer_id = request.args.get("customer_id")
            email = request.args.get("email")

            if not customer_id and not email:
                return bad_request("customer_id or email is required")

            if customer_id:
                c = session.query(Customer).filter_by(id=customer_id, brand_id=brand_id).first()
            else:
                c = session.query(Customer).filter_by(email=email, brand_id=brand_id).first()

            if not c:
                return ok({
                    "customer_id": customer_id or email,
                    "status": "not_found",
                    "can_login": False,
                    "message": "No account found. Customer may have registered with a different email.",
                })

            return ok({
                "customer_id": c.id,
                "name": c.name,
                "email": c.email,
                "status": "active",
                "tier": c.tier,
                "can_login": True,
                "total_orders": session.query(Order).filter_by(customer_id=c.id).count(),
                "message": f"Account found. Status: active. Tier: {c.tier}.",
            })
        finally:
            session.close()

    # ── Health check ────────────────────────────────────────────────────────

    @app.route("/health", methods=["GET"])
    def health():
        return ok({"status": "ok", "brands": list({} for _ in [])})

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--db", default="ecommerce.db")
    args = parser.parse_args()

    app = make_app(args.db)
    print(f"Starting demo API on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
