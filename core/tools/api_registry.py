"""
Brand API Registry — curl parser + config management.

Brands can register their APIs in two ways:
1. Curl paste: paste a curl command copied from Postman/Insomnia
2. Manual config: structured form with endpoint details

All credentials are encrypted at rest using Fernet.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from core.utils.http import http_request
from cryptography.fernet import Fernet
from sqlalchemy import text

import os as _os

from config import settings


def _get_fernet() -> "Fernet":
    """Lazily initialize Fernet from config. Fails in production if FERNET_KEY is unset."""
    key_raw = settings.fernet_key
    if not key_raw:
        raise RuntimeError(
            "FERNET_KEY env var must be set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    from cryptography.fernet import Fernet
    return Fernet(key_raw.encode())


_fernet_instance: "Fernet | None" = None


def _get_fernet_instance() -> "Fernet":
    global _fernet_instance
    if _fernet_instance is None:
        _fernet_instance = _get_fernet()
    return _fernet_instance


def _enc(value: str) -> bytes:
    return _get_fernet_instance().encrypt(value.encode())


def _dec(data: bytes) -> str:
    return _get_fernet_instance().decrypt(data).decode()


# ── Config models ───────────────────────────────────────────────────────────────

@dataclass
class ParsedCurl:
    """Result of parsing a curl command."""
    method: str
    url: str
    headers: dict[str, str]
    body: dict | None
    auth: dict | None  # {type: api_key|bearer|basic, token/key/username, password?}


@dataclass
class EndpointSpec:
    """A registered API endpoint."""
    id: str
    brand_api_config_id: str
    name: str
    description: str
    method: str
    path: str
    query_params: list[dict]
    body_schema: dict | None
    response_schema: dict | None
    rate_limit_per_min: int | None
    requires_auth: bool
    tags: list[str]
    is_active: bool
    connector_type: str = "http"
    connector_config: dict | None = None


@dataclass
class APIToolSpec:
    """A tool spec ready for dynamic tool creation."""
    name: str
    description: str
    method: str
    base_url: str
    path: str
    query_params: list[dict]
    body_schema: dict | None
    requires_auth: bool
    tags: list[str]
    connector_type: str = "http"
    connector_config: dict | None = None


# ── Curl parser ─────────────────────────────────────────────────────────────────

def parse_curl(curl_cmd: str) -> ParsedCurl:
    """
    Parse a curl command string into a ParsedCurl dataclass.

    Supports:
    - curl -X POST https://api.example.com/orders -H "Authorization: Bearer XXX"
    - curl 'https://api.example.com/customers?id=123' -H 'Content-Type: application/json' -d '{"name":"Alice"}'
    - curl -X GET https://api.example.com/customers -H "X-API-Key: secret"
    """
    cmd = curl_cmd.strip()
    if cmd.startswith("curl "):
        cmd = cmd[5:].strip()
    if cmd.startswith("'"):
        cmd = cmd[1:]
    if cmd.endswith("'"):
        cmd = cmd[:-1]

    # Extract method
    method = "GET"
    if "-X " in cmd or "--request " in cmd:
        m = re.search(r"(?:-X|--request)\s+(\w+)", cmd)
        if m:
            method = m.group(1).upper()

    # Extract URL — find first unquoted https:// or http://
    url_match = re.search(r"['\"]?(https?://[^\s'\"]+)['\"]?", cmd)
    if not url_match:
        raise ValueError("Could not parse URL from curl command")
    raw_url = url_match.group(1).rstrip("'\"")
    parsed = urlparse(raw_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path
    if parsed.query:
        path += f"?{parsed.query}"

    # Extract headers
    headers: dict[str, str] = {}
    auth: dict | None = None
    body: dict | None = None

    header_matches = re.findall(r"-H\s+['\"]([^'\"]+)['\"]", cmd)
    for h in header_matches:
        if ":" not in h:
            continue
        key, _, val = h.partition(":")
        key = key.strip()
        val = val.strip().strip("'\"")

        if key.lower() == "authorization":
            if val.startswith("Bearer "):
                auth = {"type": "bearer", "token": val[7:]}
            elif val.startswith("Basic "):
                import base64
                try:
                    decoded = base64.b64decode(val[6:]).decode()
                    user, _, pw = decoded.partition(":")
                    auth = {"type": "basic", "username": user, "password": pw}
                except Exception:
                    auth = {"type": "api_key", "key": val}
            elif val.startswith("ApiKey "):
                auth = {"type": "api_key", "key": val[7:]}
        elif key.lower() == "x-api-key":
            auth = {"type": "api_key", "key": val}
        else:
            headers[key] = val

    # Extract body
    body_match = re.search(r"--data(-raw)?\s+['\"](.+?)['\"]", cmd, re.DOTALL)
    if not body_match:
        body_match = re.search(r"-d\s+['\"](.+?)['\"]", cmd, re.DOTALL)
    if body_match:
        try:
            body = json.loads(body_match.group(2))
        except json.JSONDecodeError:
            body = {"raw": body_match.group(2)}

    return ParsedCurl(
        method=method,
        url=raw_url,
        headers=headers,
        body=body,
        auth=auth,
    )


def describe_from_curl(curl_cmd: str) -> str:
    """Extract a human-readable description from a curl command's URL and method."""
    p = parse_curl(curl_cmd)
    path_parts = [p.strip("/") for p in p.url.split("/") if p.strip("/")]
    if path_parts:
        resource = path_parts[-1].split("?")[0]
        return f"{p.method} {p.url.split('/')[-1].split('?')[0]} — {p.method} request to {p.url}"
    return f"{p.method} request"


# ── Postgres helpers ──────────────────────────────────────────────────────────────

def _get_pool():
    """Get connection from global sync pool using ConnectionProxy."""
    from core.db.pool import get_sync_pool, ConnectionProxy
    return ConnectionProxy(get_sync_pool())


def _fernet_for(key_bytes: bytes) -> Fernet:
    return Fernet(key_bytes)


# ── API config management ───────────────────────────────────────────────────────

def register_brand_api(
    brand_id: str,
    base_url: str,
    description: str = "",
    default_headers: dict | None = None,
) -> str:
    """Register a brand's API config. Returns config UUID."""
    conn = _get_pool()
    try:
        with conn:
            with conn.cursor() as cur:
                # Upsert
                cur.execute("""
                    INSERT INTO brand_api_config (id, brand_id, base_url, description, default_headers, is_active)
                    VALUES (gen_random_uuid(), %(brand_id)s, %(base_url)s, %(description)s, %(default_headers)s, true)
                    ON CONFLICT (brand_id) DO UPDATE SET
                        base_url = EXCLUDED.base_url,
                        description = EXCLUDED.description,
                        default_headers = EXCLUDED.default_headers,
                        updated_at = NOW()
                    RETURNING id
                """, {
                    "brand_id": brand_id,
                    "base_url": base_url,
                    "description": description,
                    "default_headers": json.dumps(default_headers or {}),
                })
                return str(cur.fetchone()[0])
    finally:
        conn.close()


def register_endpoint(
    brand_api_config_id: str,
    name: str,
    description: str,
    method: str,
    path: str,
    query_params: list[dict] | None = None,
    body_schema: dict | None = None,
    response_schema: dict | None = None,
    rate_limit_per_min: int | None = None,
    requires_auth: bool = False,
    tags: list[str] | None = None,
    curl_command: str | None = None,
    connector_type: str = "http",
    connector_config: dict | None = None,
) -> str:
    """
    Register a single endpoint. Accepts either explicit params or a curl command.

    For curl commands, the method, URL, body are auto-extracted.
    """
    if curl_command:
        parsed = parse_curl(curl_command)
        method = parsed.method
        # Strip base URL to get path
        from urllib.parse import urlparse as up
        p = up(parsed.url)
        path = p.path
        if p.query:
            path += f"?{p.query}"
        if not description:
            description = describe_from_curl(curl_command)
        if parsed.body and not body_schema:
            body_schema = {"type": "object", "example": parsed.body}

    conn = _get_pool()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO registered_api_endpoints
                        (id, brand_api_config_id, name, description, method, path,
                         query_params, body_schema, response_schema, rate_limit_per_min,
                         requires_auth, tags, is_active, connector_type, connector_config)
                    VALUES (gen_random_uuid(), %(config_id)s, %(name)s, %(desc)s, %(method)s, %(path)s,
                            %(query_params)s, %(body_schema)s, %(response_schema)s,
                            %(rate_limit)s, %(requires_auth)s, %(tags)s, true, %(connector_type)s, %(connector_config)s)
                    ON CONFLICT (brand_api_config_id, name) DO UPDATE SET
                        description = EXCLUDED.description,
                        method = EXCLUDED.method,
                        path = EXCLUDED.path,
                        query_params = EXCLUDED.query_params,
                        body_schema = EXCLUDED.body_schema,
                        response_schema = EXCLUDED.response_schema,
                        rate_limit_per_min = EXCLUDED.rate_limit_per_min,
                        requires_auth = EXCLUDED.requires_auth,
                        tags = EXCLUDED.tags,
                        connector_type = EXCLUDED.connector_type,
                        connector_config = EXCLUDED.connector_config,
                        updated_at = NOW()
                    RETURNING id
                """, {
                    "config_id": brand_api_config_id,
                    "name": name,
                    "desc": description,
                    "method": method.upper(),
                    "path": path,
                    "query_params": json.dumps(query_params or []),
                    "body_schema": json.dumps(body_schema) if body_schema else None,
                    "response_schema": json.dumps(response_schema) if response_schema else None,
                    "rate_limit": rate_limit_per_min,
                    "requires_auth": requires_auth,
                    "tags": tags or [],
                    "connector_type": connector_type,
                    "connector_config": json.dumps(connector_config) if connector_config else None,
                })
                return str(cur.fetchone()[0])
    finally:
        conn.close()


def store_credentials(
    brand_api_config_id: str,
    key_type: str,  # api_key | bearer_token | basic_auth | oauth2
    key_name: str,
    value: str,
) -> str:
    """Store an encrypted credential for a brand's API config."""
    encrypted = _enc(value)
    conn = _get_pool()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO brand_api_credentials
                        (id, brand_api_config_id, key_type, key_name, encrypted_value, is_active)
                    VALUES (gen_random_uuid(), %(config_id)s, %(key_type)s, %(key_name)s, %(enc_val)s, true)
                    ON CONFLICT (brand_api_config_id, key_type) DO UPDATE SET
                        encrypted_value = EXCLUDED.encrypted_value,
                        key_name = EXCLUDED.key_name,
                        is_active = true
                    RETURNING id
                """, {
                    "config_id": brand_api_config_id,
                    "key_type": key_type,
                    "key_name": key_name,
                    "enc_val": encrypted,
                })
                return str(cur.fetchone()[0])
    finally:
        conn.close()


def get_brand_config(brand_id: str) -> dict | None:
    """Get brand API config by brand_id."""
    conn = _get_pool()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, brand_id, base_url, description, default_headers, is_active
                    FROM brand_api_config WHERE brand_id = %(brand_id)s
                """, {"brand_id": brand_id})
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": str(row[0]),
                    "brand_id": row[1],
                    "base_url": row[2],
                    "description": row[3],
                    "default_headers": json.loads(row[4]) if row[4] else {},
                    "is_active": row[5],
                }
    finally:
        conn.close()


def list_endpoints_for_brand(brand_id: str) -> list[EndpointSpec]:
    """List all active endpoints for a brand's API config."""
    conn = _get_pool()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT e.id, e.brand_api_config_id, e.name, e.description,
                           e.method, e.path, e.query_params, e.body_schema,
                           e.response_schema, e.rate_limit_per_min, e.requires_auth,
                           e.tags, e.is_active, e.connector_type, e.connector_config
                    FROM registered_api_endpoints e
                    JOIN brand_api_config c ON c.id = e.brand_api_config_id
                    WHERE c.brand_id = %(brand_id)s AND e.is_active = true
                """, {"brand_id": brand_id})
                rows = cur.fetchall()
                return [
                    EndpointSpec(
                        id=str(r[0]),
                        brand_api_config_id=str(r[1]),
                        name=r[2],
                        description=r[3] or "",
                        method=r[4],
                        path=r[5],
                        query_params=json.loads(r[6]) if r[6] else [],
                        body_schema=json.loads(r[7]) if r[7] else None,
                        response_schema=json.loads(r[8]) if r[8] else None,
                        rate_limit_per_min=r[9],
                        requires_auth=r[10],
                        tags=r[11] or [],
                        is_active=r[12],
                        connector_type=r[13] if len(r) > 13 and r[13] else "http",
                        connector_config=json.loads(r[14]) if len(r) > 14 and r[14] else None,
                    )
                    for r in rows
                ]
    finally:
        conn.close()


def delete_endpoint(endpoint_id: str) -> bool:
    """Soft-delete an endpoint."""
    conn = _get_pool()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE registered_api_endpoints SET is_active = false, updated_at = NOW()
                    WHERE id = %(id)s::uuid
                """, {"id": endpoint_id})
                return cur.rowcount > 0
    finally:
        conn.close()


def test_endpoint(endpoint_id: str, test_params: dict | None = None) -> dict:
    """
    Test an endpoint by firing a real request.
    For GET: uses test_params as query params.
    For POST: uses test_params as JSON body.
    Returns the response data and status.
    """
    conn = _get_pool()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT e.id, e.method, e.path, e.query_params, e.body_schema, e.requires_auth,
                           c.base_url, c.default_headers
                    FROM registered_api_endpoints e
                    JOIN brand_api_config c ON c.id = e.brand_api_config_id
                    WHERE e.id = %(id)s::uuid AND e.is_active = true AND c.is_active = true
                """, {"id": endpoint_id})
                row = cur.fetchone()
                if not row:
                    return {"error": "Endpoint not found"}

                _, method, path, query_params, body_schema, requires_auth, base_url, default_headers = row

                # Look up brand_id from config for brand-prefixed APIs
                cur.execute("""
                    SELECT c.brand_id FROM brand_api_config c
                    JOIN registered_api_endpoints e ON e.brand_api_config_id = c.id
                    WHERE e.id = %(id)s::uuid
                """, {"id": endpoint_id})
                brand_id_row = cur.fetchone()
                brand_id = brand_id_row[0] if brand_id_row else ""
                headers = json.loads(default_headers) if default_headers else {}

                if requires_auth:
                    cur.execute("""
                        SELECT encrypted_value FROM brand_api_credentials
                        WHERE brand_api_config_id = (
                            SELECT brand_api_config_id FROM registered_api_endpoints WHERE id = %(id)s::uuid
                        ) AND is_active = true ORDER BY created_at DESC LIMIT 1
                    """, {"id": endpoint_id})
                    cred_row = cur.fetchone()
                    if cred_row:
                        token = _dec(bytes(cred_row[0]))
                        headers["Authorization"] = f"Bearer {token}"

        # Build URL — substitute path parameters from test_params
        from urllib.parse import urlencode
        base = base_url.rstrip("/")
        path = path or "/"
        # Substitute {param} in path with values from test_params
        if test_params:
            for k, v in test_params.items():
                path = path.replace(f"{{{k}}}", str(v))
        # Prepend brand_id for brand-prefixed APIs (demo API pattern)
        if path.startswith("/api/v1/"):
            path = f"/{brand_id}{path}"
        url = base + path
        if method == "GET":
            get_params = {k: v for k, v in (test_params or {}).items() if f"{{{k}}}" not in (path or "")}
            if get_params:
                url = f"{url}?{urlencode(get_params)}"

        body = None
        if method in ("POST", "PUT", "PATCH") and test_params:
            body = json.dumps(test_params)
            headers["Content-Type"] = "application/json"

        try:
            resp = http_request(method, url, headers=headers, data=body, timeout=10)
            return {
                "status_code": resp.status_code,
                "ok": resp.ok,
                "body": resp.text[:1000],
            }
        except requests.RequestException as e:
            return {"error": str(e)}
    finally:
        conn.close()


# ── Tool manifest builder ──────────────────────────────────────────────────────

def build_tool_manifest_for_brand(brand_id: str) -> list[APIToolSpec]:
    """
    Build a list of APIToolSpec for all active endpoints of a brand.
    Used by the dynamic tool builder to create LLM-callable tools.
    """
    config = get_brand_config(brand_id)
    if not config:
        return []

    endpoints = list_endpoints_for_brand(brand_id)
    return [
        APIToolSpec(
            name=ep.name,
            description=ep.description or f"Call {ep.method} {ep.path}",
            method=ep.method,
            base_url=config["base_url"],
            path=ep.path,
            query_params=ep.query_params,
            body_schema=ep.body_schema,
            requires_auth=ep.requires_auth,
            tags=ep.tags,
            connector_type=ep.connector_type,
            connector_config=ep.connector_config,
        )
        for ep in endpoints
    ]
