"""
Seed all 13 brands into the demo SQLite database.
Run: python seed.py
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

from models import (
    Base, Customer, Product, Order, OrderItem, Shipment, Return, ReturnItem, Promo,
    make_engine, make_session,
)

UTC = timezone.utc
now = lambda: datetime.now(UTC)


def _days_ago(n: int) -> datetime:
    return now() - timedelta(days=n)


def _hours_ago(n: int) -> datetime:
    return now() - timedelta(hours=n)


def _days_from_now(n: int) -> datetime:
    return now() + timedelta(days=n)


def _make_tracking_id(carrier: str) -> str:
    prefix = {"Evri": "EVRI", "DHL": "DHL", "Royal Mail": "RM", "Yodel": "YD", "UPS": "1Z"}
    return f"{prefix.get(carrier, 'TRK')}-{random.randint(100000000, 999999999)}"


BRANDS = {
    "boohoo_uk": {"name": "Boohoo", "currency": "GBP", "tier_default": "standard"},
    "boohooman_uk": {"name": "BoohooMAN", "currency": "GBP", "tier_default": "standard"},
    "plt_uk": {"name": "PrettyLittleThing", "currency": "GBP", "tier_default": "loyalty"},
    "nastygal_us": {"name": "Nasty Gal", "currency": "USD", "tier_default": "standard"},
    "karenmillen_uk": {"name": "Karen Millen", "currency": "GBP", "tier_default": "vip"},
    "dorothyperkins_uk": {"name": "Dorothy Perkins", "currency": "GBP", "tier_default": "standard"},
    "wallis_uk": {"name": "Wallis", "currency": "GBP", "tier_default": "standard"},
    "debenhams_uk": {"name": "Debenhams", "currency": "GBP", "tier_default": "standard"},
    "oasis_uk": {"name": "Oasis", "currency": "GBP", "tier_default": "standard"},
    "warehouse_uk": {"name": "Warehouse", "currency": "GBP", "tier_default": "standard"},
    "coast_uk": {"name": "Coast", "currency": "GBP", "tier_default": "standard"},
    "misspap_uk": {"name": "MissPap", "currency": "GBP", "tier_default": "standard"},
    "burton_uk": {"name": "Burton", "currency": "GBP", "tier_default": "standard"},
}

FIRST_NAMES = ["Alice", "Sophie", "Emma", "Olivia", "Mia", "Isla", "Ava", "Lily", "Charlotte", "Grace",
               "James", "Oliver", "Harry", "Jack", "George", "Noah", "Charlie", "Freddie", "Oscar", "Archie"]
LAST_NAMES = ["Thompson", "Clarke", "Wilson", "Taylor", "Brown", "Patel", "Jones", "Williams",
              "Johnson", "Martin", "Davies", "Miller", "Hughes", "Edwards", "Hill", "Green"]
CITIES_UK = ["London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Liverpool", "Newcastle",
             "Bristol", "Sheffield", "Nottingham", "Southampton", "Leicester"]
CITIES_US = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix", "San Antonio", "San Diego"]

CATEGORIES = ["Dresses", "Tops", "Trousers", "Jackets", "Coats", "Skirts", "Jeans", "Footwear", "Accessories"]

PRODUCTS = {
    "boohoo_uk": [
        {"sku": "BH-DR-001", "name": "Floral Wrap Midi Dress", "category": "Dresses", "price_gbp": 35.99, "price_usd": 45.00, "sizes": ["UK 6", "UK 8", "UK 10", "UK 12", "UK 14"]},
        {"sku": "BH-DR-002", "name": "Satin Maxi Dress", "category": "Dresses", "price_gbp": 42.99, "price_usd": 54.00, "sizes": ["UK 6", "UK 8", "UK 10", "UK 12"]},
        {"sku": "BH-TP-001", "name": "Ribbed Crop Top", "category": "Tops", "price_gbp": 12.99, "price_usd": 16.00, "sizes": ["XS", "S", "M", "L", "XL"]},
        {"sku": "BH-TP-002", "name": "Linen Blend Blouse", "category": "Tops", "price_gbp": 24.99, "price_usd": 31.00, "sizes": ["UK 6", "UK 8", "UK 10", "UK 12", "UK 14"]},
        {"sku": "BH-JK-001", "name": "Denim Trucker Jacket", "category": "Jackets", "price_gbp": 38.99, "price_usd": 49.00, "sizes": ["S", "M", "L"]},
        {"sku": "BH-PL-001", "name": "Wide Leg Trousers", "category": "Trousers", "price_gbp": 28.99, "price_usd": 36.00, "sizes": ["UK 6", "UK 8", "UK 10", "UK 12", "UK 14"]},
        {"sku": "BH-SW-001", "name": "Bikini Set Tropical", "category": "Swimwear", "price_gbp": 18.99, "price_usd": 24.00, "sizes": ["S", "M", "L"]},
    ],
    "plt_uk": [
        {"sku": "PLT-DR-001", "name": "Sequin Mini Dress", "category": "Dresses", "price_gbp": 28.99, "price_usd": 36.00, "sizes": ["UK 4", "UK 6", "UK 8", "UK 10", "UK 12"]},
        {"sku": "PLT-TP-001", "name": "Bodysuit V-Neck", "category": "Tops", "price_gbp": 14.99, "price_usd": 19.00, "sizes": ["XS", "S", "M", "L"]},
        {"sku": "PLT-JN-001", "name": "Ripped Mom Jeans", "category": "Jeans", "price_gbp": 32.99, "price_usd": 41.00, "sizes": ["UK 6", "UK 8", "UK 10", "UK 12", "UK 14"]},
    ],
    "karenmillen_uk": [
        {"sku": "KM-CO-001", "name": "Tailored Wool Coat", "category": "Coats", "price_gbp": 189.00, "price_usd": 238.00, "sizes": ["UK 6", "UK 8", "UK 10", "UK 12"]},
        {"sku": "KM-DR-001", "name": "Silk Midi Dress", "category": "Dresses", "price_gbp": 129.00, "price_usd": 162.00, "sizes": ["UK 6", "UK 8", "UK 10", "UK 12", "UK 14"]},
        {"sku": "KM-BL-001", "name": "Camel Blazer", "category": "Jackets", "price_gbp": 159.00, "price_usd": 200.00, "sizes": ["UK 6", "UK 8", "UK 10", "UK 12"]},
    ],
}

CARRIERS_UK = ["Evri", "DHL", "Royal Mail", "Yodel"]
CARRIERS_US = ["UPS", "FedEx", "USPS"]


def _customer_for_brand(brand_id: str, idx: int):
    c = BRANDS[brand_id]
    currency = c["currency"]
    city = random.choice(CITIES_US if "us" in brand_id else CITIES_UK)
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    email_domain = f"{first.lower()}{last.lower()}{idx}@example.com"
    return Customer(
        id=f"cust_{brand_id}_{idx:03d}",
        brand_id=brand_id,
        email=email_domain,
        name=f"{first} {last}",
        phone=f"+44 7{random.randint(100, 999)} {random.randint(1000000, 9999999)}" if currency == "GBP" else f"+1 {random.randint(200, 999)}-{random.randint(100, 999)}-{random.randint(1000, 9999)}",
        tier=random.choice(["standard", "standard", "loyalty", "vip"]) if random.random() > 0.5 else c["tier_default"],
        locale="en-GB" if currency == "GBP" else "en-US",
    )


def _product_for_brand(brand_id: str, sku: str, data: dict):
    c = BRANDS[brand_id]
    return Product(
        sku=sku,
        brand_id=brand_id,
        name=data["name"],
        category=data["category"],
        price_gbp=data["price_gbp"],
        price_usd=data.get("price_usd"),
        in_stock=random.random() > 0.1,
        size_options=data["sizes"],
        image_url=f"https://{c['name'].lower().replace(' ', '')}.com/images/{sku}.jpg",
    )


def _order_items(order_id: str, items_data: list, session) -> list[OrderItem]:
    items = []
    for item in items_data:
        order_item = OrderItem(
            id=f"oi_{uuid.uuid4().hex[:10]}",
            order_id=order_id,
            product_sku=item["sku"],
            product_name=item["name"],
            quantity=item.get("qty", 1),
            price_paid=item["price"],
            size=item.get("size"),
        )
        items.append(order_item)
        session.add(order_item)
    return items


def _shipment_for_order(order_id: str, brand_id: str, status: str) -> Shipment:
    c = BRANDS[brand_id]
    carriers = CARRIERS_US if c["currency"] == "USD" else CARRIERS_UK
    carrier = random.choice(carriers)
    tracking_id = _make_tracking_id(carrier)

    events = []
    dispatched = None
    estimated = None
    actual = None

    if status in ("dispatched", "in_transit", "delivered"):
        dispatched = _days_ago(random.randint(3, 7))
        estimated = _days_from_now(random.randint(1, 3))
        events.append({"timestamp": dispatched.isoformat(), "location": "Warehouse", "status": "Dispatched"})

    if status in ("in_transit", "delivered"):
        transit_time = _days_ago(random.randint(1, 2))
        events.append({"timestamp": transit_time.isoformat(), "location": "Distribution Centre", "status": "In Transit"})

    if status == "in_transit":
        events.append({"timestamp": _days_ago(0).isoformat(), "location": "Local Depot", "status": "Out for Delivery"})

    if status == "delivered":
        actual = _days_ago(random.randint(0, 1))
        events.append({"timestamp": actual.isoformat(), "location": "Delivered to neighbour", "status": "Delivered"})

    return Shipment(
        id=f"ship_{uuid.uuid4().hex[:10]}",
        order_id=order_id,
        carrier=carrier,
        tracking_id=tracking_id,
        tracking_url=f"https://{carrier.lower().replace(' ', '')}.com/track/{tracking_id}",
        status=status if status != "dispatched" else "in_transit",
        dispatched_at=dispatched,
        estimated_delivery=estimated,
        actual_delivery=actual,
        events=events,
    )


def seed_all(session):
    print("Seeding products...")
    for brand_id, products in PRODUCTS.items():
        for prod_data in products:
            session.add(Product(
                sku=prod_data["sku"],
                brand_id=brand_id,
                name=prod_data["name"],
                category=prod_data["category"],
                price_gbp=prod_data["price_gbp"],
                price_usd=prod_data.get("price_usd"),
                in_stock=random.random() > 0.1,
                size_options=prod_data["sizes"],
            ))

    print("Seeding customers, orders, and returns...")

    for brand_id in BRANDS:
        products = PRODUCTS.get(brand_id, PRODUCTS["boohoo_uk"])
        currency = BRANDS[brand_id]["currency"]

        for idx in range(1, 6):  # 5 customers per brand
            customer = _customer_for_brand(brand_id, idx)
            session.add(customer)

            # 2-4 orders per customer
            num_orders = random.randint(2, 4)
            for oi in range(1, num_orders + 1):
                # Various order statuses
                status_weights = ["processing", "processing", "dispatched", "in_transit", "delivered", "delivered", "returned"]
                status = random.choice(status_weights)
                days_ago = random.randint(1, 45)
                placed = _days_ago(days_ago)

                # Build items
                num_items = random.randint(1, 3)
                items_data = []
                total = 0.0
                for _ in range(num_items):
                    prod = random.choice(products)
                    qty = random.randint(1, 2)
                    price = prod["price_gbp"] if currency == "GBP" else prod.get("price_usd", prod["price_gbp"])
                    total += price * qty
                    items_data.append({
                        "sku": prod["sku"],
                        "name": prod["name"],
                        "price": price,
                        "qty": qty,
                        "size": random.choice(prod["sizes"]),
                    })

                order_id = f"ORD-{random.randint(100000, 999999)}"
                promo_code = random.choice([None, None, f"SAVE10_{brand_id[:2].upper()}", "NEWCUST20"])

                order = Order(
                    id=order_id,
                    brand_id=brand_id,
                    customer_id=customer.id,
                    status=status,
                    placed_at=placed,
                    total_value=total,
                    currency=currency,
                    payment_method=random.choice(["card", "card", "paypal", "klarna", "afterpay"]),
                    shipping_address={
                        "line1": f"{random.randint(1, 200)} {random.choice(['High St', 'Main Rd', 'Oak Ave', 'Park Lane'])}",
                        "city": random.choice(CITIES_UK if currency == "GBP" else CITIES_US),
                        "postcode": f"{random.choice(['SW1A', 'M1 ', 'B1 ', 'LS1', 'G1'])}{random.randint(1, 9)}{random.randint(0, 9)}" if currency == "GBP" else str(random.randint(10001, 99999)),
                        "country": "United Kingdom" if currency == "GBP" else "United States",
                    },
                    promo_code=promo_code,
                    discount_applied=round(total * 0.1, 2) if promo_code else 0.0,
                )
                session.add(order)

                # Add order items
                for item_data in items_data:
                    session.add(OrderItem(
                        id=f"oi_{uuid.uuid4().hex[:10]}",
                        order_id=order_id,
                        product_sku=item_data["sku"],
                        product_name=item_data["name"],
                        quantity=item_data["qty"],
                        price_paid=item_data["price"],
                        size=item_data.get("size"),
                    ))

                # Add shipment for non-processing orders
                if status != "processing":
                    shipment = _shipment_for_order(order_id, brand_id, status)
                    session.add(shipment)

                # Add return for returned orders
                if status == "returned":
                    reason = random.choice(["wrong_item", "damaged_item", "changed_mind", "not_as_described"])
                    return_id = f"RET-{uuid.uuid4().hex[:8].upper()}"
                    received_days_ago = random.randint(3, 10)

                    ret = Return(
                        id=return_id,
                        brand_id=brand_id,
                        order_id=order_id,
                        customer_id=customer.id,
                        status="completed",
                        initiated_at=placed + timedelta(days=random.randint(2, 5)),
                        received_at=_days_ago(received_days_ago),
                        refund_initiated_at=_days_ago(received_days_ago - 1),
                        refund_sent_at=_days_ago(received_days_ago - 3),
                        reason=reason,
                        return_tracking_id=_make_tracking_id(random.choice(CARRIERS_UK)),
                        refund_amount=total,
                        refund_method="original",
                        refund_reference=f"REF-{random.randint(100000, 999999)}",
                    )
                    session.add(ret)

                # Add active return for some in-transit orders
                if status == "in_transit" and random.random() > 0.7:
                    reason = random.choice(["wrong_item", "changed_mind"])
                    return_id = f"RET-{uuid.uuid4().hex[:8].upper()}"
                    ret = Return(
                        id=return_id,
                        brand_id=brand_id,
                        order_id=order_id,
                        customer_id=customer.id,
                        status=random.choice(["initiated", "label_sent", "in_transit"]),
                        initiated_at=now() - timedelta(hours=random.randint(2, 24)),
                        reason=reason,
                        refund_amount=total,
                    )
                    session.add(ret)

    # Seed promos
    print("Seeding promos...")
    for brand_id in BRANDS:
        session.add(Promo(
            code=f"SAVE10_{brand_id[:2].upper()}",
            brand_id=brand_id,
            discount_type="percent",
            discount_value=10,
            min_spend=30.0,
            active=True,
            expires_at=_days_from_now(60),
        ))
        session.add(Promo(
            code=f"WELCOME_{brand_id[:2].upper()}_{random.randint(10, 30)}",
            brand_id=brand_id,
            discount_type="percent",
            discount_value=random.randint(10, 20),
            min_spend=0.0,
            active=True,
            expires_at=_days_from_now(30),
        ))
        session.add(Promo(
            code=f"EXPIRED2024_{brand_id[:2].upper()}",
            brand_id=brand_id,
            discount_type="percent",
            discount_value=5,
            min_spend=0.0,
            active=False,
            expires_at=_days_ago(30),
        ))

    session.commit()
    print(f"Seed complete. Brands: {len(BRANDS)}, Products: {session.query(Product).count()}, Customers: {session.query(Customer).count()}, Orders: {session.query(Order).count()}")


if __name__ == "__main__":
    engine = make_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = make_session(engine)
    seed_all(session)
    session.close()
    print("Done!")
