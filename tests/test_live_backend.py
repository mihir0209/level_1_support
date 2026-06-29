"""Tests for the dynamic, resilient live backend client and config loader."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from cryptography.fernet import Fernet
from core.tools.backends.config_loader import (
    clear_config_cache,
    get_brand_api_client_config,
)
from core.tools.backends.live_backend import LiveBackend, _CIRCUIT_BREAKERS


@pytest.fixture(autouse=True)
def setup_fernet_key(monkeypatch):
    """Inject a valid Fernet key into settings for testing."""
    from config import settings
    test_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "fernet_key", test_key)

    # Force reset the cached lazy singleton in registry
    import core.tools.api_registry as reg
    monkeypatch.setattr(reg, "_fernet_instance", None)


def test_live_backend_fallback_when_no_config(monkeypatch):
    """Verify that LiveBackend falls back to MockBackend when no config exists."""
    clear_config_cache()
    monkeypatch.setattr("core.tools.backends.live_backend.LiveBackend._get_config", lambda self, brand_id: None)

    backend = LiveBackend()
    # Query a valid mock order so fallback returns found = True
    res = backend.lookup_order("BH-12345-UK", "boohoo_uk")
    assert res is not None
    assert "found" in res
    assert res["found"] is True  # fallback mock order found


def test_config_loader_decryption(monkeypatch):
    """Verify that config loader fetches config and decrypts credentials from the database."""
    from core.tools.api_registry import _enc

    # 1. Setup mock database cursor to return API config and encrypted credential
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "config_uuid_123",
        "https://api.testbrand.com",
        json.dumps({"X-Brand-Region": "UK"}),
    )

    encrypted_key = _enc("secret_token_val")
    mock_cursor.fetchall.return_value = [
        ("bearer_token", "AuthKey", encrypted_key)
    ]

    # 2. Setup mock psycopg connection and pool context managers
    mock_conn = MagicMock()

    class CursorCM:
        def __enter__(self):
            return mock_cursor

        def __exit__(self, et, ev, tb):
            pass

    mock_conn.cursor.return_value = CursorCM()

    class PoolCM:
        def __enter__(self):
            return mock_conn

        def __exit__(self, et, ev, tb):
            pass

    mock_pool = MagicMock()
    mock_pool.connection.return_value = PoolCM()

    monkeypatch.setattr("core.tools.backends.config_loader.get_sync_pool", lambda: mock_pool)

    clear_config_cache()
    cfg = get_brand_api_client_config("test_brand_dec")
    assert cfg is not None
    assert cfg["base_url"] == "https://api.testbrand.com"
    assert cfg["headers"]["X-Brand-Region"] == "UK"
    assert cfg["headers"]["Authorization"] == "Bearer secret_token_val"


def test_live_backend_circuit_breaker_and_retries(monkeypatch):
    """Verify that transient 5xx errors trigger retries and trip the circuit breaker."""
    # Reset circuit breakers state
    _CIRCUIT_BREAKERS.clear()

    # Mock settings to ensure valid keys exist
    monkeypatch.setattr("core.tools.backends.live_backend.LiveBackend._get_config", lambda self, brand_id: {
        "base_url": "https://api.testbrand.com",
        "headers": {"Authorization": "Bearer key"}
    })

    # Disable sleep delay during testing
    monkeypatch.setattr("time.sleep", lambda secs: None)

    # Mock httpx.Client to return a 503 error
    mock_client = MagicMock()
    mock_client.request.return_value = httpx.Response(status_code=503)

    class ClientCM:
        def __enter__(self):
            return mock_client

        def __exit__(self, et, ev, tb):
            pass

    monkeypatch.setattr("httpx.Client", lambda *args, **kwargs: ClientCM())

    backend = LiveBackend()

    # 1. First run: attempts 3 retries, records 1 failure
    backend.lookup_order("BH-12345-UK", "boohoo_uk")
    assert mock_client.request.call_count == 3

    mock_client.request.reset_mock()

    # 2. Second and third runs: trip the circuit breaker (failures threshold = 3)
    backend.lookup_order("BH-12345-UK", "boohoo_uk")
    res_trip = backend.lookup_order("BH-12345-UK", "boohoo_uk")
    # Falls back to MockBackend output on final trip
    assert "found" in res_trip

    mock_client.request.reset_mock()

    # 3. Fourth run: circuit is OPEN. Request is blocked from making HTTP calls and defaults to fallback directly
    res_blocked = backend.lookup_order("BH-12345-UK", "boohoo_uk")
    assert mock_client.request.call_count == 0
    assert res_blocked["found"] is True
