"""
SQLAlchemy models for the demo ecommerce API.
Mirrors a real ecommerce platform's data model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
import uuid
import json

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, ForeignKey, JSON, create_engine,
    PrimaryKeyConstraint,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class Customer(Base):
    __tablename__ = "customers"

    id = Column(String(64), primary_key=True)
    brand_id = Column(String(64), nullable=False, index=True)
    email = Column(String(256), nullable=False)
    name = Column(String(256), nullable=False)
    phone = Column(String(32), nullable=True)
    tier = Column(String(32), default="standard")  # standard, loyalty, vip
    locale = Column(String(8), default="en-GB")
    created_at = Column(DateTime(timezone=True), default=utcnow)

    orders = relationship("Order", back_populates="customer", lazy="dynamic")

    def to_dict(self, include_orders: bool = False) -> dict:
        d = {
            "id": self.id,
            "brand_id": self.brand_id,
            "email": self.email,
            "name": self.name,
            "phone": self.phone,
            "tier": self.tier,
            "locale": self.locale,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_orders:
            d["orders"] = [o.to_dict() for o in self.orders.order_by(Order.placed_at.desc()).limit(10)]
        return d


class Product(Base):
    __tablename__ = "products"

    sku = Column(String(64), primary_key=True)
    brand_id = Column(String(64), nullable=False, index=True)
    name = Column(String(512), nullable=False)
    category = Column(String(128), nullable=True)
    price_gbp = Column(Float, nullable=False)
    price_usd = Column(Float, nullable=True)
    in_stock = Column(Boolean, default=True)
    size_options = Column(JSON, default=list)  # ["XS","S","M","L","XL"]
    image_url = Column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "brand_id": self.brand_id,
            "name": self.name,
            "category": self.category,
            "price_gbp": self.price_gbp,
            "price_usd": self.price_usd,
            "in_stock": self.in_stock,
            "size_options": self.size_options or [],
            "image_url": self.image_url,
        }


class Order(Base):
    __tablename__ = "orders"

    id = Column(String(64), primary_key=True)
    brand_id = Column(String(64), nullable=False, index=True)
    customer_id = Column(String(64), ForeignKey("customers.id"), nullable=False)
    status = Column(String(32), default="processing")  # processing, confirmed, dispatched, in_transit, delivered, returned, cancelled
    placed_at = Column(DateTime(timezone=True), default=utcnow)
    total_value = Column(Float, nullable=False)
    currency = Column(String(8), default="GBP")
    payment_method = Column(String(32), default="card")  # card, paypal, klarna, afterpay, giftcard
    shipping_address = Column(JSON, default=dict)
    promo_code = Column(String(64), nullable=True)
    discount_applied = Column(Float, default=0.0)

    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", lazy="joined")
    shipment = relationship("Shipment", back_populates="order", uselist=False, lazy="joined")
    returns = relationship("Return", back_populates="order", lazy="dynamic")

    def to_dict(self, detailed: bool = False) -> dict:
        d = {
            "id": self.id,
            "brand_id": self.brand_id,
            "customer_id": self.customer_id,
            "status": self.status,
            "placed_at": self.placed_at.isoformat() if self.placed_at else None,
            "total_value": self.total_value,
            "currency": self.currency,
            "payment_method": self.payment_method,
            "promo_code": self.promo_code,
            "discount_applied": self.discount_applied,
        }
        if detailed:
            d["items"] = [i.to_dict() for i in self.items]
            d["shipment"] = self.shipment.to_dict() if self.shipment else None
        return d


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(String(64), primary_key=True)
    order_id = Column(String(64), ForeignKey("orders.id"), nullable=False)
    product_sku = Column(String(64), ForeignKey("products.sku"), nullable=False)
    product_name = Column(String(512), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    price_paid = Column(Float, nullable=False)
    size = Column(String(16), nullable=True)
    status = Column(String(32), default="active")  # active, returned, refunded, exchanged

    order = relationship("Order", back_populates="items")
    product = relationship("Product")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "product_sku": self.product_sku,
            "product_name": self.product_name,
            "quantity": self.quantity,
            "price_paid": self.price_paid,
            "size": self.size,
            "status": self.status,
        }


class Shipment(Base):
    __tablename__ = "shipments"

    id = Column(String(64), primary_key=True)
    order_id = Column(String(64), ForeignKey("orders.id"), nullable=False, unique=True)
    carrier = Column(String(64), nullable=False)
    tracking_id = Column(String(256), nullable=True)
    tracking_url = Column(Text, nullable=True)
    status = Column(String(32), default="pending")  # pending, picked_up, in_transit, out_for_delivery, delivered, returned_to_sender, failed_delivery
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    estimated_delivery = Column(DateTime(timezone=True), nullable=True)
    actual_delivery = Column(DateTime(timezone=True), nullable=True)
    events = Column(JSON, default=list)  # [{timestamp, location, status}]

    order = relationship("Order", back_populates="shipment")

    def to_dict(self) -> dict:
        events = self.events or []
        last_event = events[-1] if events else None
        return {
            "id": self.id,
            "order_id": self.order_id,
            "carrier": self.carrier,
            "tracking_id": self.tracking_id,
            "tracking_url": self.tracking_url,
            "status": self.status,
            "dispatched_at": self.dispatched_at.isoformat() if self.dispatched_at else None,
            "estimated_delivery": self.estimated_delivery.isoformat() if self.estimated_delivery else None,
            "actual_delivery": self.actual_delivery.isoformat() if self.actual_delivery else None,
            "last_event": last_event,
        }


class Return(Base):
    __tablename__ = "returns"

    id = Column(String(64), primary_key=True)
    brand_id = Column(String(64), nullable=False, index=True)
    order_id = Column(String(64), ForeignKey("orders.id"), nullable=False)
    customer_id = Column(String(64), ForeignKey("customers.id"), nullable=False)
    status = Column(String(32), default="initiated")  # initiated, label_sent, in_transit, received, inspecting, refund_processing, refund_sent, completed, rejected
    initiated_at = Column(DateTime(timezone=True), default=utcnow)
    received_at = Column(DateTime(timezone=True), nullable=True)
    refund_initiated_at = Column(DateTime(timezone=True), nullable=True)
    refund_sent_at = Column(DateTime(timezone=True), nullable=True)
    reason = Column(Text, nullable=True)  # wrong_item, damaged_item, changed_mind, defective, not_as_described
    customer_note = Column(Text, nullable=True)
    return_tracking_id = Column(String(256), nullable=True)
    return_tracking_url = Column(Text, nullable=True)
    refund_amount = Column(Float, nullable=True)
    refund_method = Column(String(32), default="original")  # original, store_credit, giftcard
    refund_reference = Column(String(128), nullable=True)
    refund_expected_by = Column(DateTime(timezone=True), nullable=True)

    order = relationship("Order", back_populates="returns")
    items = relationship("ReturnItem", back_populates="return_", lazy="joined")

    def to_dict(self, detailed: bool = False) -> dict:
        d = {
            "id": self.id,
            "brand_id": self.brand_id,
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "status": self.status,
            "initiated_at": self.initiated_at.isoformat() if self.initiated_at else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "refund_initiated_at": self.refund_initiated_at.isoformat() if self.refund_initiated_at else None,
            "refund_sent_at": self.refund_sent_at.isoformat() if self.refund_sent_at else None,
            "reason": self.reason,
            "return_tracking_id": self.return_tracking_id,
            "return_tracking_url": self.return_tracking_url,
            "refund_amount": self.refund_amount,
            "refund_method": self.refund_method,
            "refund_reference": self.refund_reference,
            "refund_expected_by": self.refund_expected_by.isoformat() if self.refund_expected_by else None,
        }
        if detailed:
            d["items"] = [i.to_dict() for i in self.items]
        return d


class ReturnItem(Base):
    __tablename__ = "return_items"

    id = Column(String(64), primary_key=True)
    return_id = Column(String(64), ForeignKey("returns.id"), nullable=False)
    order_item_id = Column(String(64), ForeignKey("order_items.id"), nullable=False)
    product_name = Column(String(512), nullable=False)
    reason = Column(String(32), nullable=True)

    return_ = relationship("Return", back_populates="items")
    order_item = relationship("OrderItem")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "return_id": self.return_id,
            "order_item_id": self.order_item_id,
            "product_name": self.product_name,
            "reason": self.reason,
        }


class Promo(Base):
    __tablename__ = "promos"

    code = Column(String(64), nullable=False)
    brand_id = Column(String(64), nullable=False, index=True)
    __table_args__ = (
        PrimaryKeyConstraint("code", "brand_id"),
    )
    discount_type = Column(String(16), default="percent")  # percent, fixed
    discount_value = Column(Float, nullable=False)
    min_spend = Column(Float, default=0.0)
    max_discount = Column(Float, nullable=True)
    active = Column(Boolean, default=True)
    starts_at = Column(DateTime(timezone=True), default=utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    usage_limit = Column(Integer, nullable=True)
    usage_count = Column(Integer, default=0)
    applies_to_all = Column(Boolean, default=True)  # if False, only specific SKUs
    excluded_categories = Column(JSON, default=list)
    single_use = Column(Boolean, default=False)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "brand_id": self.brand_id,
            "discount_type": self.discount_type,
            "discount_value": self.discount_value,
            "min_spend": self.min_spend,
            "max_discount": self.max_discount,
            "active": self.active,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "usage_limit": self.usage_limit,
            "usage_count": self.usage_count,
            "applies_to_all": self.applies_to_all,
            "excluded_categories": self.excluded_categories or [],
            "single_use": self.single_use,
        }


def make_engine(db_path: str = "ecommerce.db"):
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def make_session(engine):
    return sessionmaker(bind=engine)()
