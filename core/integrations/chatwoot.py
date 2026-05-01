"""
Chatwoot API client — creates and updates conversations on escalation.

Uses the Chatwoot Conversation API:
  POST   /api/v1/accounts/{account_id}/conversations
  PUT    /api/v1/accounts/{account_id}/conversations/{id}
  POST   /api/v1/accounts/{account_id}/conversations/{id}/messages

Bot/API key auth: X-Api-Key header.
"""

from __future__ import annotations

from typing import Any

import structlog

from config import settings

log = structlog.get_logger(__name__)


def _base_url() -> str:
    return settings.chatwoot_api_url.rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Api-Key": settings.chatwoot_api_key,
    }


def _account_id() -> int:
    return settings.chatwoot_account_id


def _require_config() -> bool:
    """Return True if Chatwoot is configured with a key and URL."""
    return bool(settings.chatwoot_api_key and settings.chatwoot_api_url)


def _request(method: str, path: str, **kwargs) -> Response | None:
    """Fire an HTTP request to Chatwoot. Returns None on config miss or error."""
    if not _require_config():
        return None
    from core.utils.http import http_request
    url = _base_url() + path
    try:
        return http_request(method, url, headers=_headers(), **kwargs)
    except Exception as e:
        log.warning("chatwoot_request_failed", method=method, path=path, error=str(e))
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

def create_conversation(
    brand_id: str,
    inbox_id: int,
    customer_name: str | None,
    customer_email: str | None,
    initial_message: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """
    Create a Chatwoot conversation for an escalation.
    Returns the conversation public_id on success, None on failure.

    The conversation is created with:
      - source: "api" (created via API, not from a widget)
      - inbox_id: brand's Chatwoot inbox
      - meta: session_id for correlation
    """
    from core.utils.http import http_request

    if not _require_config():
        log.debug("chatwoot_not_configured_skipping", brand_id=brand_id)
        return None

    url = f"{_base_url()}/api/v1/accounts/{_account_id()}/conversations"
    payload: dict[str, Any] = {
        "inbox_id": inbox_id,
        "source": {
            "type": "api",
            "initiated_at": None,
        },
        "meta": {
            "session_id": session_id,
            "brand_id": brand_id,
            **(metadata or {}),
        },
        "contact": {
            "name": customer_name or "Customer",
            "email": customer_email,
        },
        "message": {
            "content": initial_message,
        },
    }

    try:
        resp = http_request("POST", url, headers=_headers(), json_body=payload, timeout=15)
        if resp.ok:
            data = resp.json()
            conv_id = data.get("id") or data.get("public_id")
            log.info(
                "chatwoot_conversation_created",
                conversation_id=conv_id,
                inbox_id=inbox_id,
                brand_id=brand_id,
            )
            return str(conv_id) if conv_id else None
        else:
            log.warning(
                "chatwoot_conversation_create_failed",
                status=resp.status_code,
                body=resp.text[:200],
                brand_id=brand_id,
            )
            return None
    except Exception as e:
        log.error("chatwoot_conversation_create_error", error=str(e), brand_id=brand_id)
        return None


def add_message(
    conversation_id: int | str,
    content: str,
    message_type: str = "outgoing",
) -> bool:
    """
    Post a message to an existing Chatwoot conversation.
    message_type: "outgoing" (agent reply) or "incoming" (customer reply).
    """
    from core.utils.http import http_request

    if not _require_config():
        return False

    url = (
        f"{_base_url()}/api/v1/accounts/{_account_id()}/conversations"
        f"/{conversation_id}/messages"
    )
    payload = {
        "content": content,
        "message_type": message_type,
        "private": False,
    }

    try:
        resp = http_request("POST", url, headers=_headers(), json_body=payload, timeout=10)
        if resp.ok:
            return True
        log.warning(
            "chatwoot_message_post_failed",
            conversation_id=conversation_id,
            status=resp.status_code,
        )
        return False
    except Exception as e:
        log.error("chatwoot_message_post_error", error=str(e), conversation_id=conversation_id)
        return False


def resolve_conversation(conversation_id: int | str) -> bool:
    """
    Mark a Chatwoot conversation as resolved (closes the ticket).
    """
    if not _require_config():
        return False

    url = (
        f"{_base_url()}/api/v1/accounts/{_account_id()}/conversations"
        f"/{conversation_id}"
    )
    from core.utils.http import http_request
    try:
        resp = http_request("PATCH", url, headers=_headers(), json_body={"status": "resolved"}, timeout=10)
        if resp.ok:
            log.info("chatwoot_conversation_resolved", conversation_id=conversation_id)
            return True
        log.warning("chatwoot_resolve_failed", conversation_id=conversation_id, status=resp.status_code)
        return False
    except Exception as e:
        log.error("chatwoot_resolve_error", error=str(e), conversation_id=conversation_id)
        return False