"""
Unit tests for the new security and reliability infrastructure:
- Admin auth middleware
- Pydantic validation schemas
- HTTP retry + timeout utilities
- Rate limiting middleware
- Structured logging config
- OpenTelemetry tracing

Run with: uv run pytest tests/test_security.py -v
"""

from __future__ import annotations

import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, sentinel

from fastapi import FastAPI, Depends, HTTPException
from fastapi.testclient import TestClient

# ── Auth middleware tests ─────────────────────────────────────────────────────

class TestAuthMiddleware:

    def test_missing_x_admin_key_returns_401(self):
        from core.middleware.auth import verify_admin_key
        app = FastAPI()
        @app.get("/protected", dependencies=[Depends(verify_admin_key)])
        async def protected():
            return {"ok": True}

        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/protected")
        assert r.status_code == 401
        assert "Missing X-ADMIN-KEY" in r.json()["detail"]

    def test_wrong_x_admin_key_returns_403(self):
        from core.middleware.auth import verify_admin_key
        app = FastAPI()
        @app.get("/protected", dependencies=[Depends(verify_admin_key)])
        async def protected():
            return {"ok": True}

        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/protected", headers={"X-ADMIN-KEY": "wrong-key"})
        assert r.status_code == 403
        assert "Invalid X-ADMIN-KEY" in r.json()["detail"]

    def test_correct_x_admin_key_succeeds(self):
        from core.middleware.auth import verify_admin_key
        from fastapi import Depends
        app = FastAPI()
        @app.get("/protected", dependencies=[Depends(verify_admin_key)])
        async def protected():
            return {"ok": True}

        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/protected", headers={"X-ADMIN-KEY": "l1support-demo-key-2026"})
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ── Validation schema tests ─────────────────────────────────────────────────

class TestBrandIdValidation:

    def test_valid_brand_ids(self):
        from core.schemas.validation import validate_brand_id
        for bid in ["boohoo_uk", "plt_uk", "karenmillen_uk", "test123", "a", "x" * 64]:
            assert validate_brand_id(bid) == bid

    def test_invalid_brand_ids_rejected(self):
        from core.schemas.validation import validate_brand_id
        for bid in ["boohoo-uk", "boohoo uk", "boohoo.uk", "", "a" * 65, "hello world", "test@brand"]:
            with pytest.raises(ValueError, match="Invalid brand_id"):
                validate_brand_id(bid)

    def test_brand_id_max_length(self):
        from core.schemas.validation import validate_brand_id
        long_id = "a" * 64
        assert validate_brand_id(long_id) == long_id
        with pytest.raises(ValueError):
            validate_brand_id("a" * 65)


class TestGraphQueryValidation:

    def test_safe_queries_accepted(self):
        from core.schemas.validation import GraphQueryRequest
        for q in [
            "MATCH (n:Brand) RETURN n LIMIT 5",
            "MATCH (p:Policy) WHERE p.brand_id = 'boohoo_uk' RETURN p",
            "CALL db.labels()",
        ]:
            r = GraphQueryRequest(query=q)
            assert r.query == q

    def test_destructive_queries_blocked(self):
        from core.schemas.validation import GraphQueryRequest
        dangerous = [
            "MATCH (n) DETACH DELETE n",
            "DROP TABLE users",
            "MATCH (n) DELETE n",
            "REMOVE n.name",
        ]
        for q in dangerous:
            with pytest.raises(ValueError, match="forbidden keyword"):
                GraphQueryRequest(query=q)


class TestHttpMethodEnum:

    def test_valid_methods(self):
        from core.schemas.validation import HttpMethod
        for m in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            assert HttpMethod(m).value == m

    def test_invalid_method_rejected(self):
        from pydantic import BaseModel
        from core.schemas.validation import HttpMethod
        with pytest.raises(ValueError):
            HttpMethod("OPTIONS")


# ── HTTP utility tests ──────────────────────────────────────────────────────

class TestHttpRequest:

    def test_success_returns_response(self):
        from core.utils.http import http_request
        with patch("core.utils.http.requests.request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.ok = True
            mock_resp.json.return_value = {"result": "ok"}
            mock_req.return_value = mock_resp

            resp = http_request("GET", "https://example.com/api")
            assert resp.status_code == 200

    def test_4xx_does_not_retry(self):
        from core.utils.http import http_request
        with patch("core.utils.http.requests.request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.ok = False
            mock_req.return_value = mock_resp

            # Should return immediately (no retries for 4xx)
            start = time.time()
            resp = http_request("GET", "https://example.com/api", max_retries=3)
            elapsed = time.time() - start
            assert elapsed < 2  # no sleep
            assert resp.status_code == 404

    def test_5xx_retries_with_backoff(self):
        from core.utils.http import http_request
        with patch("core.utils.http.requests.request") as mock_req:
            # First 2 calls return 500, third succeeds
            responses = [
                MagicMock(status_code=500, ok=False),
                MagicMock(status_code=500, ok=False),
                MagicMock(status_code=200, ok=True),
            ]
            mock_req.side_effect = responses

            start = time.time()
            resp = http_request("GET", "https://example.com/api", max_retries=2, backoff_factor=0.01)
            elapsed = time.time() - start
            # Should have slept twice (0^ + 1^ backoff with factor=0.01 = near-zero, but calls happened)
            assert resp.status_code == 200
            assert mock_req.call_count == 3

    def test_network_error_retries(self):
        from core.utils.http import http_request
        import requests
        with patch("core.utils.http.requests.request") as mock_req:
            mock_req.side_effect = [
                requests.ConnectionError("fail 1"),
                requests.ConnectionError("fail 2"),
                MagicMock(status_code=200, ok=True),
            ]
            resp = http_request("GET", "https://example.com/api", max_retries=2, backoff_factor=0.01)
            assert resp.status_code == 200
            assert mock_req.call_count == 3

    def test_timeout_enforced(self):
        from core.utils.http import http_request
        with patch("core.utils.http.requests.request") as mock_req:
            mock_resp = MagicMock(status_code=200, ok=True)
            mock_req.return_value = mock_resp

            http_request("GET", "https://example.com/api", timeout=5)
            _, kwargs = mock_req.call_args
            assert kwargs["timeout"] == 5


# ── Logging config tests ─────────────────────────────────────────────────────

class TestLoggingConfig:

    def test_get_logger_returns_logger(self):
        from core.logging_config import get_logger, configure_logging
        configure_logging("DEBUG", "development")
        log = get_logger("test")
        assert log is not None

    def test_bind_clear_correlation(self):
        from core.logging_config import bind_correlation, clear_correlation
        bind_correlation(request_id="req-123", brand_id="boohoo_uk", session_id="sess-456")
        clear_correlation()
        # No assertion failure = ok

    def test_configure_logging_produces_no_error(self):
        from core.logging_config import configure_logging
        configure_logging("INFO", "development")
        configure_logging("DEBUG", "production")
        # No error = pass


# ── Tracing tests ──────────────────────────────────────────────────────────

class TestTracing:

    def test_get_tracer_returns_tracer(self):
        from core.tracing import get_tracer
        tracer = get_tracer()
        assert tracer is not None

    def test_inject_extract_context(self):
        from core.tracing import inject_trace_context, extract_trace_context
        carrier = inject_trace_context()
        # Should return a dict (even if empty without OTEL endpoint configured)
        assert isinstance(carrier, dict)

    def test_traced_decorator_sync(self):
        from core.tracing import traced

        @traced("test.operation")
        def sync_fn():
            return 42

        result = sync_fn()
        assert result == 42

    @pytest.mark.asyncio
    async def test_traced_decorator_async(self):
        from core.tracing import traced

        @traced("async.test")
        async def async_fn():
            return "async result"

        result = await async_fn()
        assert result == "async result"

    def test_set_span_attributes(self):
        from core.tracing import get_tracer, set_span_attributes

        tracer = get_tracer()
        with tracer.start_as_current_span("test") as span:
            set_span_attributes(span, foo="bar", count=42, flag=True)
            # No error = ok


# ── Rate limit tests ─────────────────────────────────────────────────────────

class TestRateLimitMiddleware:

    @pytest.fixture
    def app_with_rate_limit(self):
        from core.middleware.rate_limit import rate_limit_middleware
        app = FastAPI()
        app.middleware("http")(rate_limit_middleware)
        @app.get("/test")
        async def test():
            return {"ok": True}
        return app

    def test_no_ratelimit_on_first_requests(self, app_with_rate_limit):
        client = TestClient(app_with_rate_limit, raise_server_exceptions=False)
        for _ in range(5):
            r = client.get("/test")
            assert r.status_code == 200

    def test_rate_limit_headers_present(self, app_with_rate_limit):
        client = TestClient(app_with_rate_limit, raise_server_exceptions=False)
        r = client.get("/test")
        assert "X-RateLimit-Limit" in r.headers
        assert "X-RateLimit-Remaining" in r.headers
        assert "X-RateLimit-Reset" in r.headers