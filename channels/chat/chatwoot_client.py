"""
Chatwoot Channel API Client.
"""

from __future__ import annotations

import structlog
from typing import Any
from config import settings
from core.utils.http import http_request

log = structlog.get_logger(__name__)

def _base_url() -> str:
    return settings.chatwoot_api_url.rstrip("/")

def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "api_access_token": settings.chatwoot_api_key,
    }

def _account_id() -> int:
    return settings.chatwoot_account_id

def _require_config() -> bool:
    return bool(settings.chatwoot_api_key and settings.chatwoot_api_url)

def post_message(conv_id: int | str, content: str, private: bool = False) -> bool:
    """Post an agent reply or private note to a conversation."""
    if not _require_config():
        log.warning("chatwoot_not_configured", action="post_message")
        return False

    url = f"{_base_url()}/api/v1/accounts/{_account_id()}/conversations/{conv_id}/messages"
    payload = {
        "content": content,
        "message_type": "outgoing",
        "private": private,
    }

    try:
        resp = http_request("POST", url, headers=_headers(), json_body=payload)
        if resp.ok:
            return True
        log.warning("chatwoot_post_message_failed", conv_id=conv_id, status=resp.status_code)
        return False
    except Exception as e:
        log.error("chatwoot_post_message_error", conv_id=conv_id, error=str(e))
        return False

def assign_team(conv_id: int | str, team_id: int) -> bool:
    """Assign a conversation to a specific team."""
    if not _require_config():
        log.warning("chatwoot_not_configured", action="assign_team")
        return False

    url = f"{_base_url()}/api/v1/accounts/{_account_id()}/conversations/{conv_id}/assignments"
    payload = {
        "team_id": team_id,
    }

    try:
        resp = http_request("POST", url, headers=_headers(), json_body=payload)
        if resp.ok:
            log.info("chatwoot_conversation_team_assigned", conv_id=conv_id, team_id=team_id)
            return True
        log.warning("chatwoot_assign_team_failed", conv_id=conv_id, status=resp.status_code)
        return False
    except Exception as e:
        log.error("chatwoot_assign_team_error", conv_id=conv_id, error=str(e))
        return False

def add_label(conv_id: int | str, label: str) -> bool:
    """Add a label to a conversation, keeping existing ones."""
    if not _require_config():
        log.warning("chatwoot_not_configured", action="add_label")
        return False

    labels_url = f"{_base_url()}/api/v1/accounts/{_account_id()}/conversations/{conv_id}/labels"

    # Get current labels
    existing_labels = []
    try:
        get_resp = http_request("GET", labels_url, headers=_headers())
        if get_resp.ok:
            data = get_resp.json()
            existing_labels = data.get("payload", [])
    except Exception as e:
        log.warning("chatwoot_get_labels_error", conv_id=conv_id, error=str(e))

    # Merge labels
    if label not in existing_labels:
        existing_labels.append(label)

    payload = {
        "labels": existing_labels,
    }

    try:
        resp = http_request("POST", labels_url, headers=_headers(), json_body=payload)
        if resp.ok:
            log.info("chatwoot_add_label_success", conv_id=conv_id, label=label)
            return True
        log.warning("chatwoot_add_label_failed", conv_id=conv_id, status=resp.status_code)
        return False
    except Exception as e:
        log.error("chatwoot_add_label_error", conv_id=conv_id, error=str(e))
        return False

def create_contact(name: str, email: str | None = None, phone: str | None = None) -> int | None:
    """Create a contact and return their numeric contact ID."""
    if not _require_config():
        log.warning("chatwoot_not_configured", action="create_contact")
        return None

    # Search if contact exists first by email
    if email:
        search_url = f"{_base_url()}/api/v1/accounts/{_account_id()}/contacts/search"
        try:
            search_resp = http_request("GET", search_url, headers=_headers(), params={"q": email})
            if search_resp.ok:
                data = search_resp.json()
                payload = data.get("payload", [])
                if payload:
                    contact_id = payload[0].get("id")
                    log.info("chatwoot_contact_found", email=email, contact_id=contact_id)
                    return contact_id
        except Exception as e:
            log.warning("chatwoot_search_contact_failed", email=email, error=str(e))

    # Create new contact
    url = f"{_base_url()}/api/v1/accounts/{_account_id()}/contacts"
    payload: dict[str, Any] = {
        "name": name,
    }
    if email:
        payload["email"] = email
    if phone:
        payload["phone_number"] = phone

    try:
        resp = http_request("POST", url, headers=_headers(), json_body=payload)
        if resp.ok:
            data = resp.json()
            if isinstance(data, dict):
                if "id" in data:
                    return data["id"]
                payload_data = data.get("payload")
                if isinstance(payload_data, list) and payload_data:
                    return payload_data[0].get("id")
                elif isinstance(payload_data, dict):
                    return payload_data.get("id")
            log.warning("chatwoot_create_contact_no_id", data=data)
            return None
        log.warning("chatwoot_create_contact_failed", status=resp.status_code)
        return None
    except Exception as e:
        log.error("chatwoot_create_contact_error", name=name, error=str(e))
        return None

def get_conversation(conv_id: int | str) -> dict[str, Any] | None:
    """Fetch details and history of a specific conversation."""
    if not _require_config():
        log.warning("chatwoot_not_configured", action="get_conversation")
        return None

    url = f"{_base_url()}/api/v1/accounts/{_account_id()}/conversations/{conv_id}"

    try:
        resp = http_request("GET", url, headers=_headers())
        if resp.ok:
            return resp.json()
        log.warning("chatwoot_get_conversation_failed", conv_id=conv_id, status=resp.status_code)
        return None
    except Exception as e:
        log.error("chatwoot_get_conversation_error", conv_id=conv_id, error=str(e))
        return None


import hmac
import hashlib
import time

def verify_webhook_signature(
    raw_body: bytes,
    timestamp: str,
    received_signature: str,
    secret: str
) -> bool:
    """
    Verify Chatwoot webhook signature with HMAC-SHA256 and replay protection.
    """
    if not secret:
        log.warning("chatwoot_signature_verification_skipped_no_secret")
        return False

    # Replay protection: check if timestamp is within 5 minutes (300 seconds)
    try:
        ts_val = float(timestamp)
        now = time.time()
        if abs(now - ts_val) > 300:
            log.warning("chatwoot_webhook_replay_protection", timestamp=timestamp, current_time=now)
            return False
    except (ValueError, TypeError) as e:
        log.warning("chatwoot_webhook_invalid_timestamp", timestamp=timestamp, error=str(e))
        return False

    # Construct signature payload: {timestamp}.{raw_body}
    message = f"{timestamp}.".encode("utf-8") + raw_body

    computed_digest = hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256
    ).hexdigest()

    expected_signature = f"sha256={computed_digest}"

    return hmac.compare_digest(expected_signature, received_signature)

