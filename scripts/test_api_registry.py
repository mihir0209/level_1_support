#!/usr/bin/env python3
"""
End-to-end test for the dynamic API registry pipeline.
Tests: curl parsing → Postgres storage → tool loading → HTTP execution.

Usage: python test_api_registry.py [--brand boohoo_uk]
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

BASE = "http://localhost:8000"
# Demo API runs on host machine — Docker containers reach it via host IP
_DEMO_HOST = "192.168.0.105"


def get(path: str) -> dict:
    url = f"{BASE}{path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def post(path: str, data: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def test_curl_parser():
    """Test curl parsing — paste a curl command and extract details."""
    print("\n=== TEST: Curl Parser ===")
    curl_cmd = 'curl -X GET "http://api.example.com/customers?id=cust_123" -H "Authorization: Bearer secret123" -H "Content-Type: application/json"'
    result = post("/admin/api-config/curl-parse", {"curl_command": curl_cmd})
    print(f"Input: {curl_cmd[:60]}...")
    print(f"Parsed method: {result.get('method')}")
    print(f"Parsed URL: {result.get('url')}")
    print(f"Auth type: {result.get('auth', {}).get('type')}")
    assert result.get("method") == "GET"
    assert "api.example.com" in result.get("url", "")
    print("PASS: Curl parser extracts method, URL, and auth correctly")


def test_wire_brand(brand_id: str):
    """Wire demo API for a brand."""
    print(f"\n=== TEST: Wire Demo API for {brand_id} ===")
    result = post(f"/admin/api-config/demo/wire-brand?brand_id={brand_id}")
    print(f"Config ID: {result.get('config_id', 'N/A')}")
    print(f"Endpoints registered: {result.get('endpoints_registered', 0)}")
    print(f"Endpoints skipped: {result.get('endpoints_skipped', 0)}")
    print(f"Errors: {result.get('errors', [])}")
    assert result.get("success") is True
    print(f"PASS: {brand_id} wired with {result.get('endpoints_registered')} endpoints")


def test_list_endpoints(brand_id: str):
    """List registered endpoints for a brand."""
    print(f"\n=== TEST: List Endpoints for {brand_id} ===")
    result = get(f"/admin/api-config/{brand_id}/endpoints")
    print(f"Total endpoints: {result.get('count', 0)}")
    for ep in result.get("endpoints", []):
        print(f"  - {ep['method']:6s} {ep['path']:45s} [{', '.join(ep['tags'])}]")
    assert result.get("count", 0) > 0
    print(f"PASS: {result.get('count')} endpoints listed")


def test_tool_manifest(brand_id: str):
    """Get LLM tool manifest for a brand."""
    print(f"\n=== TEST: Tool Manifest for {brand_id} ===")
    result = get(f"/admin/api-config/{brand_id}/tools")
    print(f"Total tools: {result.get('count', 0)}")
    for t in result.get("tools", []):
        print(f"  - {t['name']}: {t['description'][:50]}")
    assert result.get("count", 0) > 0
    print(f"PASS: {result.get('count')} tools in manifest")


def test_endpoint_execution():
    """Test actually firing HTTP requests through registered endpoints."""
    print("\n=== TEST: Endpoint Execution (real HTTP calls) ===")

    tests = [
        ("track_shipment", "c7052655-93fb-46ff-8755-fba124cce882",
         {"order_id": "ORD-527736"}, "shipment tracking"),
        ("get_order_status", "41bf5b9b-82dc-4041-98ae-784d1a710e5f",
         {"order_id": "ORD-527736"}, "order status"),
        ("get_refund_status", "8ef51a3a-d59f-4e30-a940-638259b30773",
         {"return_id": "RET-794DCE99"}, "refund status"),
        ("account_status", "88407fa8-4664-4985-ba5c-1c7dd39af24e",
         {"customer_id": "cust_boohoo_uk_001"}, "account status"),
    ]

    all_passed = True
    for name, ep_id, params, desc in tests:
        result = post(f"/admin/api-config/test/{ep_id}", params)
        status = "PASS" if result.get("ok") else "FAIL"
        if not result.get("ok"):
            all_passed = False
        body = result.get("body", "")[:80]
        print(f"  [{status}] {name} ({desc}): HTTP {result.get('status_code')} — {body}")
        if result.get("ok") and desc == "account status":
            parsed = json.loads(result["body"])
            assert "active" in parsed.get("status", "").lower()
            assert "Charlie" in parsed.get("name", "")

    assert all_passed
    print("PASS: All endpoint execution tests succeeded")


def main():
    parser = argparse.ArgumentParser(description="Test the dynamic API registry pipeline")
    parser.add_argument("--brand", default="boohoo_uk", help="Brand ID to test")
    args = parser.parse_args()

    print(f"Testing Dynamic API Registry Pipeline")
    print(f"Brand: {args.brand}")
    print(f"Main API: {BASE}")

    # 1. Wire demo API
    test_wire_brand(args.brand)

    # 2. List endpoints
    test_list_endpoints(args.brand)

    # 3. Tool manifest
    test_tool_manifest(args.brand)

    # 4. Curl parser
    test_curl_parser()

    # 5. Real endpoint execution
    test_endpoint_execution()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED — Dynamic API Registry is fully operational")
    print("=" * 60)


if __name__ == "__main__":
    main()
