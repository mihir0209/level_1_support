"""
Dynamic tool builder — converts registered API specs into LLM-callable tools.

Each tool is a @tool-decorated function that:
1. Accepts typed parameters matching the endpoint's query_params / body_schema
2. Merges brand credentials and default headers
3. Fires the HTTP request against the registered base_url + path
4. Returns parsed JSON (or an error string)

These tools are loaded per-brand in the LangGraph pipeline and bound via bind_tools().
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

import psycopg
import requests
from langchain_core.tools import tool

from core.tools.api_registry import (
    APIToolSpec,
    build_tool_manifest_for_brand,
    _dec as _fernet_dec,
)
from core.utils.http import http_request
from core.tracing import traced, set_span_attributes
from core.logging_config import get_logger

log = get_logger(__name__)


# ── Tool registry ────────────────────────────────────────────────────────────

_TOOL_REGISTRY: dict[str, Callable] = {}


def _build_tool_func(spec: APIToolSpec) -> Callable:
    """
    Factory: given an APIToolSpec, build a @tool-decorated function.

    The function name, docstring, and parameters are all derived from the spec.
    """
    # Build a safe function name from base spec name
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", spec.name)

    # Build docstring
    params_desc = ""
    if spec.query_params:
        param_lines = []
        for p in spec.query_params:
            required = "required" if p.get("required") else "optional"
            param_lines.append(f"  {p['name']} ({p.get('type', 'string')}, {required}): {p.get('description', '')}")
        params_desc = "\n\nArgs:\n" + "\n".join(param_lines)
    elif spec.body_schema:
        params_desc = f"\n\nArgs:\n  **kwargs: body parameters per schema"

    doc = f"""Call the {spec.method} {spec.path} endpoint for {spec.name}.

Endpoint: {spec.description or f'{spec.method} {spec.path}'}
Tags: {', '.join(spec.tags) if spec.tags else 'general'}{params_desc}
    """

    def _execute_tool(kwargs: dict | None = None, **kw) -> str:
        """
        Fire the HTTP request. Credentials are decrypted per brand.
        Returns JSON string on success, error string on failure.
        """
        conn = _get_pool()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT c.base_url, c.default_headers, c.brand_id
                        FROM brand_api_config c
                        JOIN registered_api_endpoints e ON e.brand_api_config_id = c.id
                        WHERE e.name = %(name)s AND e.is_active = true AND c.is_active = true
                        LIMIT 1
                    """, {"name": tool_name})

                    row = cur.fetchone()
                    if not row:
                        return f"Error: brand API config not found for {tool_name}"
                    base_url, default_headers_raw, brand_id_for_url = row
                    headers = json.loads(default_headers_raw) if default_headers_raw else {}
        finally:
            conn.close()

        all_params = {}
        if kwargs:
            all_params.update(kwargs)
        all_params.update(kw)

        if spec.requires_auth:
            cred = _get_credential_for_spec(tool_name)
            if cred:
                if cred["type"] == "bearer":
                    headers["Authorization"] = f"Bearer {cred['value']}"
                elif cred["type"] == "api_key":
                    headers["X-API-Key"] = cred["value"]
                elif cred["type"] == "basic":
                    import base64
                    encoded = base64.b64encode(f"{cred['username']}:{cred['password']}".encode()).decode()
                    headers["Authorization"] = f"Basic {encoded}"

        # Build URL — substitute path params, handle brand-prefixed APIs
        path = spec.path or "/"
        # Substitute path parameters {param} with values from all_params
        for k, v in list(all_params.items()):
            if f"{{{k}}}" in path:
                path = path.replace(f"{{{k}}}", str(v))
                del all_params[k]
        # Prepend brand_id for brand-prefixed APIs (demo API pattern)
        if path.startswith("/api/v1/"):
            path = f"/{brand_id_for_url}{path}"
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"

        query_params = {
            p["name"]: all_params.pop(p["name"])
            for p in (spec.query_params or [])
            if p["name"] in all_params
        }

        body = dict(all_params)

        try:
            span_name = f"tool.http.{spec.method.lower()}"
            with get_tracer().start_as_current_span(span_name) as span:
                set_span_attributes(
                    span,
                    tool_name=spec.name,
                    http_method=spec.method,
                    http_url=url,
                    brand_id=brand_id_for_url,
                    requires_auth=spec.requires_auth,
                )
                resp = http_request(
                    spec.method,
                    url,
                    headers=headers,
                    params=query_params or None,
                    json_body=body if body and spec.method in ("POST", "PUT", "PATCH") else None,
                    timeout=15,
                )
                set_span_attributes(span, http_status_code=resp.status_code)
                if not resp.ok:
                    log.warning(
                        "tool_http_failed",
                        tool=spec.name,
                        status=resp.status_code,
                        url=url,
                    )
            if resp.ok:
                try:
                    return json.dumps(resp.json(), indent=2)
                except json.JSONDecodeError:
                    return resp.text
            else:
                return f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as e:
            return f"Request failed: {e}"

    endpoint_name = spec.name  # capture for closure
    _execute_tool.__name__ = safe_name
    _execute_tool.__doc__ = doc

    return _execute_tool


def _get_pool():
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "l1_support"),
        user=os.environ.get("POSTGRES_USER", "l1support"),
        password=os.environ.get("POSTGRES_PASSWORD", "l1support_secret"),
        autocommit=True,
    )


def _get_credential_for_spec(tool_name: str) -> dict | None:
    """Decrypt and return credential for a tool's brand config."""
    conn = _get_pool()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT cred.key_type, cred.key_name,
                           cred.encrypted_value,
                           c.id
                    FROM brand_api_credentials cred
                    JOIN registered_api_endpoints e ON e.brand_api_config_id = cred.brand_api_config_id
                    JOIN brand_api_config c ON c.id = cred.brand_api_config_id
                    WHERE e.name = %(name)s AND cred.is_active = true
                    LIMIT 1
                """, {"name": tool_name})
                row = cur.fetchone()
                if not row:
                    return None
                key_type, key_name, enc_raw, _ = row
                value = _fernet_dec(bytes(enc_raw))
                return {"type": key_type, "name": key_name, "value": value}
    finally:
        conn.close()


# ── Public API ─────────────────────────────────────────────────────────────────

def load_tools_for_brand(brand_id: str, brand_fernet_key: bytes | None = None) -> list:
    """
    Load all dynamic tools for a brand.

    Returns a list of @tool-decorated functions ready to be bound to the LLM
    via `model.bind_tools(tools)`.

    Tools are cached in _TOOL_REGISTRY keyed by tool name.
    """
    manifest = build_tool_manifest_for_brand(brand_id)
    tools = []
    for spec in manifest:
        # Tool name = brand_id_prefixed to avoid collisions across brands
        tool_name = f"{brand_id}_{spec.name}"
        cache_key = tool_name
        if cache_key in _TOOL_REGISTRY:
            tools.append(_TOOL_REGISTRY[cache_key])
            continue

        func = _build_tool_func(spec)
        # Re-wrap with @tool (langchain_core.tool decorator)
        try:
            decorated = tool(func)
        except Exception:
            # If @tool fails (e.g. args don't match), use raw func
            decorated = func

        _TOOL_REGISTRY[cache_key] = decorated
        tools.append(decorated)

    return tools


def clear_tool_cache(brand_id: str | None = None):
    """Clear cached tools. Pass brand_id to clear only that brand's tools."""
    if brand_id:
        prefix = f"{brand_id}_"
        for k in list(_TOOL_REGISTRY.keys()):
            if k.startswith(prefix):
                del _TOOL_REGISTRY[k]
    else:
        _TOOL_REGISTRY.clear()


def get_tool_manifest_summary(brand_id: str) -> list[dict]:
    """Human-readable summary of available tools for a brand."""
    manifest = build_tool_manifest_for_brand(brand_id)
    return [
        {
            "name": f"{brand_id}_{spec.name}",
            "description": spec.description,
            "method": spec.method,
            "path": spec.path,
            "tags": spec.tags,
            "requires_auth": spec.requires_auth,
            "params": [p["name"] for p in spec.query_params] if spec.query_params else [],
        }
        for spec in manifest
    ]
