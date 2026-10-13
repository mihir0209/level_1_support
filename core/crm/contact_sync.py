"""Chatwoot Webhook Contact Synchronization.

Extracts customer properties (name, email, phone) from incoming Chatwoot
webhook payloads and synchronizes them to the local PostgreSQL ontology
registry, linking conversations with validated customer profiles.
"""

from __future__ import annotations

import json
import uuid
import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)


def extract_contact_from_payload(payload: dict) -> dict | None:
    """
    Parse a Chatwoot webhook payload and extract the sender contact fields.

    Returns a dict with keys: name, email, phone, chatwoot_contact_id
    or None if no useful contact information is present.
    """
    sender = payload.get("sender") or {}
    conversation = payload.get("conversation") or {}
    contact_inbox = conversation.get("contact_inbox") or {}

    # Chatwoot embeds contact details at multiple levels
    name = sender.get("name") or sender.get("custom_attributes", {}).get("name")
    email = (
        sender.get("email")
        or contact_inbox.get("source_id")  # some inboxes use source_id as email
    )
    phone = sender.get("phone_number") or sender.get("phone")
    chatwoot_contact_id = str(sender.get("id", "")) if sender.get("id") else None

    if not email and not phone and not name:
        return None

    return {
        "name": name or "Unknown",
        "email": email,
        "phone": phone,
        "chatwoot_contact_id": chatwoot_contact_id,
    }


async def resolve_or_create_customer(
    brand_id: str,
    contact: dict,
) -> str:
    """
    Look up a customer in PostgreSQL by email or phone.
    If not found, create a new record. Returns the customer_id.
    """
    email = contact.get("email")
    phone = contact.get("phone")
    name = contact.get("name", "Unknown")

    try:
        from core.db.pool import get_async_pool

        pool = get_async_pool()
        async with pool.connection() as conn:
            # Try email match first
            if email:
                row = await (
                    await conn.execute(
                        "SELECT id FROM customers WHERE brand_id = %s AND email = %s LIMIT 1",
                        (brand_id, email),
                    )
                ).fetchone()
                if row:
                    log.info("customer_resolved_by_email", customer_id=str(row[0]), email=email)
                    return str(row[0])

            # Try phone match
            if phone:
                row = await (
                    await conn.execute(
                        "SELECT id FROM customers WHERE brand_id = %s AND phone = %s LIMIT 1",
                        (brand_id, phone),
                    )
                ).fetchone()
                if row:
                    log.info("customer_resolved_by_phone", customer_id=str(row[0]), phone=phone)
                    return str(row[0])

            # No match — create new customer
            customer_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO customers (id, brand_id, name, email, phone)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (customer_id, brand_id, name, email, phone),
            )
            log.info("customer_created", customer_id=customer_id, brand_id=brand_id, email=email)
            return customer_id

    except Exception as exc:
        log.warning("customer_db_sync_failed_using_redis_fallback", error=str(exc))

    # Redis fallback: store contact mapping when DB is unavailable
    return await _redis_fallback_resolve(brand_id, contact)


async def _redis_fallback_resolve(brand_id: str, contact: dict) -> str:
    """Fallback resolution using Redis when PostgreSQL is unreachable."""
    r = get_redis()
    email = contact.get("email")
    phone = contact.get("phone")

    # Try email key
    if email:
        existing = await r.get(f"contact:{brand_id}:email:{email}")
        if existing:
            return existing

    # Try phone key
    if phone:
        existing = await r.get(f"contact:{brand_id}:phone:{phone}")
        if existing:
            return existing

    # Create new
    customer_id = str(uuid.uuid4())
    if email:
        await r.set(f"contact:{brand_id}:email:{email}", customer_id)
    if phone:
        await r.set(f"contact:{brand_id}:phone:{phone}", customer_id)

    # Store contact metadata
    await r.set(
        f"contact:{brand_id}:{customer_id}",
        json.dumps(contact),
    )
    log.info("customer_created_redis_fallback", customer_id=customer_id, brand_id=brand_id)
    return customer_id


async def sync_contact_from_webhook(payload: dict, brand_id: str, session_id: str) -> str | None:
    """
    End-to-end pipeline: extract contact from Chatwoot payload,
    resolve/create in DB, and link to session.

    Returns the resolved customer_id or None if no contact data was present.
    """
    contact = extract_contact_from_payload(payload)
    if not contact:
        return None

    customer_id = await resolve_or_create_customer(brand_id, contact)

    # Link customer to session in Redis
    from core.memory.session import update_session
    await update_session(session_id, {"customer_id": customer_id})

    log.info(
        "contact_synced_to_session",
        session_id=session_id,
        customer_id=customer_id,
        brand_id=brand_id,
        email=contact.get("email"),
    )
    return customer_id
