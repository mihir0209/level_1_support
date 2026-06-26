import os
import json
import time
import hmac
import hashlib
import pytest
from unittest.mock import MagicMock, patch

from channels.chat.inbox_router import route_inbox
from channels.chat.chatwoot_client import (
    verify_webhook_signature,
    post_message,
    assign_team,
    add_label,
    create_contact,
    get_conversation
)
from config import settings

# ─────────────────────────────────────────────────────────────────────────────
# 1. INBOX ROUTING TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_route_inbox_env_override(monkeypatch):
    # Setup env override
    mapping = {"100": "plt_uk", "200": "boohooman_uk"}
    monkeypatch.setenv("CHATWOOT_INBOX_TO_BRAND", json.dumps(mapping))

    assert route_inbox(100) == "plt_uk"
    assert route_inbox("200") == "boohooman_uk"
    assert route_inbox(999) == "boohoo_uk"  # Fallback

def test_route_inbox_registry_fallback(monkeypatch):
    # Ensure env variable is cleared
    monkeypatch.delenv("CHATWOOT_INBOX_TO_BRAND", raising=False)

    from brands.registry import BrandEntry, register_brand
    test_brand = BrandEntry(
        brand_id="test_brand",
        name="Test Brand",
        domains=["test.com"],
        locales=["en-GB"],
        default_region="UK",
        chatwoot_inbox_id=456
    )
    register_brand(test_brand)

    assert route_inbox(456) == "test_brand"
    assert route_inbox(999) == "boohoo_uk"  # Fallback

# ─────────────────────────────────────────────────────────────────────────────
# 2. WEBHOOK SIGNATURE VERIFICATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

def test_verify_webhook_signature_success():
    secret = "secret123"
    timestamp = str(int(time.time()))
    raw_body = b'{"event":"message_created","content":"hello"}'
    
    # Calculate expected signature
    message = f"{timestamp}.".encode("utf-8") + raw_body
    sig_hash = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    signature = f"sha256={sig_hash}"

    assert verify_webhook_signature(raw_body, timestamp, signature, secret) is True

def test_verify_webhook_signature_invalid():
    secret = "secret123"
    timestamp = str(int(time.time()))
    raw_body = b'{"event":"message_created","content":"hello"}'
    signature = "sha256=invalidhash"

    assert verify_webhook_signature(raw_body, timestamp, signature, secret) is False

def test_verify_webhook_signature_replay_protection():
    secret = "secret123"
    # Set timestamp to 10 minutes ago
    timestamp = str(int(time.time()) - 600)
    raw_body = b'{"event":"message_created","content":"hello"}'
    
    message = f"{timestamp}.".encode("utf-8") + raw_body
    sig_hash = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    signature = f"sha256={sig_hash}"

    assert verify_webhook_signature(raw_body, timestamp, signature, secret) is False

def test_verify_webhook_signature_no_secret():
    assert verify_webhook_signature(b"{}", "123456", "sha256=hash", "") is False

# ─────────────────────────────────────────────────────────────────────────────
# 3. CLIENT API METHODS TESTS
# ─────────────────────────────────────────────────────────────────────────────

@patch("channels.chat.chatwoot_client._require_config", return_value=True)
@patch("channels.chat.chatwoot_client.http_request")
def test_post_message(mock_http, mock_config):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_http.return_value = mock_resp

    success = post_message("123", "Hello customer", private=False)
    assert success is True

    mock_http.assert_called_once()
    args, kwargs = mock_http.call_args
    assert "/conversations/123/messages" in args[1]
    assert kwargs["json_body"]["content"] == "Hello customer"
    assert kwargs["json_body"]["private"] is False

@patch("channels.chat.chatwoot_client._require_config", return_value=True)
@patch("channels.chat.chatwoot_client.http_request")
def test_assign_team(mock_http, mock_config):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_http.return_value = mock_resp

    success = assign_team("123", 42)
    assert success is True

    mock_http.assert_called_once()
    args, kwargs = mock_http.call_args
    assert "/conversations/123/assignments" in args[1]
    assert kwargs["json_body"]["team_id"] == 42

@patch("channels.chat.chatwoot_client._require_config", return_value=True)
@patch("channels.chat.chatwoot_client.http_request")
def test_add_label(mock_http, mock_config):
    # Setup mock to return existing labels: {"payload": ["billing"]}
    mock_get_resp = MagicMock()
    mock_get_resp.ok = True
    mock_get_resp.json.return_value = {"payload": ["billing"]}
    
    mock_post_resp = MagicMock()
    mock_post_resp.ok = True
    
    mock_http.side_effect = [mock_get_resp, mock_post_resp]

    success = add_label("123", "wismo")
    assert success is True

    assert mock_http.call_count == 2
    # Verify GET labels call
    get_args, _ = mock_http.call_args_list[0]
    assert get_args[0] == "GET"
    assert "/conversations/123/labels" in get_args[1]

    # Verify POST labels call
    post_args, post_kwargs = mock_http.call_args_list[1]
    assert post_args[0] == "POST"
    assert "/conversations/123/labels" in post_args[1]
    assert set(post_kwargs["json_body"]["labels"]) == {"billing", "wismo"}

@patch("channels.chat.chatwoot_client._require_config", return_value=True)
@patch("channels.chat.chatwoot_client.http_request")
def test_create_contact(mock_http, mock_config):
    # Mock search response to return empty, then mock create contact response
    mock_search_resp = MagicMock()
    mock_search_resp.ok = True
    mock_search_resp.json.return_value = {"payload": []}

    mock_create_resp = MagicMock()
    mock_create_resp.ok = True
    mock_create_resp.json.return_value = {
        "id": 999,
        "name": "Alice",
        "email": "alice@example.com"
    }

    mock_http.side_effect = [mock_search_resp, mock_create_resp]

    contact_id = create_contact("Alice", "alice@example.com")
    assert contact_id == 999

    assert mock_http.call_count == 2

@patch("channels.chat.chatwoot_client._require_config", return_value=True)
@patch("channels.chat.chatwoot_client.http_request")
def test_get_conversation(mock_http, mock_config):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": 123, "status": "resolved"}
    mock_http.return_value = mock_resp

    conv = get_conversation("123")
    assert conv == {"id": 123, "status": "resolved"}

    mock_http.assert_called_once_with("GET", mock_http.call_args[0][1], headers=mock_http.call_args[1]["headers"])
