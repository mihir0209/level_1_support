"""
Database-driven configuration loader for dynamic tool backends.
Resolves brand API base URLs and decrypts credentials to construct request headers.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import structlog
from core.db.pool import get_sync_pool
from core.tools.api_registry import _dec as _fernet_dec

log = structlog.get_logger(__name__)

# Cache configurations in memory for 1 minute (60,000 ms) to avoid database roundtrips on every HTTP request
_CONFIG_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_CACHE_TTL_MS = 60000


def get_brand_api_client_config(brand_id: str) -> dict[str, Any] | None:
    """
    Resolve base URL and decrypt authorization credentials for a brand.
    Combines headers from brand_api_config default_headers and decrypted brand_api_credentials.
    """
    now_ms = time.monotonic() * 1000
    if brand_id in _CONFIG_CACHE:
        cached_config, cached_at = _CONFIG_CACHE[brand_id]
        if now_ms - cached_at < _CACHE_TTL_MS:
            return cached_config

    pool = get_sync_pool()
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # 1. Fetch brand_api_config
                cur.execute("""
                    SELECT id, base_url, default_headers
                    FROM brand_api_config
                    WHERE brand_id = %(brand_id)s AND is_active = true
                    LIMIT 1
                """, {"brand_id": brand_id})
                config_row = cur.fetchone()
                if not config_row:
                    return None

                config_id, base_url, default_headers = config_row
                if isinstance(default_headers, str):
                    import json
                    headers = json.loads(default_headers)
                else:
                    headers = default_headers or {}

                # 2. Fetch all active credentials for this config
                cur.execute("""
                    SELECT key_type, key_name, encrypted_value
                    FROM brand_api_credentials
                    WHERE brand_api_config_id = %(config_id)s AND is_active = true
                """, {"config_id": config_id})
                cred_rows = cur.fetchall()

                credentials = []
                for cred_row in cred_rows:
                    key_type, key_name, enc_raw = cred_row
                    try:
                        value = _fernet_dec(bytes(enc_raw))
                        credentials.append({
                            "type": key_type,
                            "name": key_name,
                            "value": value
                        })
                    except Exception as dec_err:
                        log.warning(
                            "decrypt_credential_failed",
                            brand_id=brand_id,
                            key_name=key_name,
                            error=str(dec_err)
                        )

                # Inject decrypted auth headers based on credential types
                client_headers = {**headers}
                for cred in credentials:
                    k_type = cred["type"]
                    k_name = cred["name"]
                    k_val = cred["value"]
                    if k_type == "bearer_token":
                        client_headers["Authorization"] = f"Bearer {k_val}"
                    elif k_type == "api_key":
                        client_headers[k_name] = k_val
                    elif k_type == "basic_auth":
                        encoded = base64.b64encode(k_val.encode()).decode()
                        client_headers["Authorization"] = f"Basic {encoded}"

                config_res = {
                    "base_url": base_url.rstrip("/"),
                    "headers": client_headers,
                    "credentials": credentials
                }
                _CONFIG_CACHE[brand_id] = (config_res, now_ms)
                return config_res

    except Exception as e:
        log.error("get_brand_api_client_config_failed", brand_id=brand_id, error=str(e))
        return None


def clear_config_cache(brand_id: str | None = None) -> None:
    """Clear the local configuration cache. Useful in tests or on credentials rotation."""
    global _CONFIG_CACHE
    if brand_id:
        _CONFIG_CACHE.pop(brand_id, None)
    else:
        _CONFIG_CACHE.clear()
