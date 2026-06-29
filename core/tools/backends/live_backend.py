"""
Live WMS/OMS API connector backend.
Dynamically loads client base URLs and headers from PostgreSQL configuration,
decrypts auth credentials, and performs resilient requests with backoff and circuit-breaking.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
from core.tools.backends.interface import ToolBackend
from core.tools.backends.mock_backend import MockBackend

log = structlog.get_logger(__name__)

# Track circuit breaker state in memory per brand
_CIRCUIT_BREAKERS: dict[str, dict[str, Any]] = {}
_FAILURE_THRESHOLD = 3
_COOLDOWN_PERIOD_SECONDS = 30


class LiveBackend(ToolBackend):
    """
    Resilient multi-tenant Live API backend routing requests to dynamic brand-specific endpoints.
    Falls back to MockBackend logic if live calls fail, timing out, or are unconfigured.
    """

    def __init__(self):
        self.fallback = MockBackend()

    def _get_config(self, brand_id: str) -> dict[str, Any] | None:
        from core.tools.backends.config_loader import get_brand_api_client_config
        return get_brand_api_client_config(brand_id)

    def _check_circuit_breaker(self, brand_id: str) -> bool:
        cb = _CIRCUIT_BREAKERS.setdefault(brand_id, {
            "state": "CLOSED",
            "failures": 0,
            "last_failure_at": 0.0
        })
        if cb["state"] == "OPEN":
            # Check if cooldown period has passed
            if time.time() - cb["last_failure_at"] > _COOLDOWN_PERIOD_SECONDS:
                log.info("circuit_breaker_half_open", brand_id=brand_id)
                cb["state"] = "HALF-OPEN"
                return True
            return False
        return True

    def _record_success(self, brand_id: str) -> None:
        cb = _CIRCUIT_BREAKERS.get(brand_id)
        if cb:
            cb["failures"] = 0
            if cb["state"] == "HALF-OPEN":
                log.info("circuit_breaker_closed", brand_id=brand_id)
                cb["state"] = "CLOSED"

    def _record_failure(self, brand_id: str) -> None:
        cb = _CIRCUIT_BREAKERS.setdefault(brand_id, {
            "state": "CLOSED",
            "failures": 0,
            "last_failure_at": 0.0
        })
        cb["failures"] += 1
        cb["last_failure_at"] = time.time()
        if cb["failures"] >= _FAILURE_THRESHOLD:
            log.error("circuit_breaker_opened", brand_id=brand_id, failures=cb["failures"])
            cb["state"] = "OPEN"

    def _make_request(
        self,
        brand_id: str,
        method: str,
        path: str,
        **kwargs
    ) -> httpx.Response | None:
        cfg = self._get_config(brand_id)
        if not cfg:
            return None

        base_url = cfg["base_url"]
        headers = {**cfg["headers"], "Content-Type": "application/json"}
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))

        url = f"{base_url}/{path.lstrip('/')}"

        # Exponential backoff retry configurations
        max_retries = 3
        backoff_factor = 1.5

        if not self._check_circuit_breaker(brand_id):
            log.warning("circuit_breaker_blocked_request", brand_id=brand_id, url=url)
            raise httpx.HTTPError(f"Circuit breaker is OPEN for brand {brand_id}")

        for attempt in range(max_retries):
            try:
                # W3C Trace context propagation
                from core.tracing import inject_trace_context
                headers.update(inject_trace_context())

                with httpx.Client(timeout=5.0) as client:
                    response = client.request(method, url, headers=headers, **kwargs)

                    # Return immediately on client success or client errors
                    if response.status_code < 500:
                        self._record_success(brand_id)
                        return response

                    log.warning("request_transient_error", code=response.status_code, attempt=attempt + 1, url=url)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                log.warning("request_timeout_error", error=str(exc), attempt=attempt + 1, url=url)

            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)

        # Record failure to trigger circuit breaker if all retries are exhausted
        self._record_failure(brand_id)
        return None

    def lookup_order(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("live_backend_called", method="lookup_order", order_id=order_id, brand_id=brand_id)
        try:
            res = self._make_request(brand_id, "GET", f"/orders/{order_id}")
            if res:
                if res.status_code == 200:
                    return {"found": True, **res.json()}
                elif res.status_code == 404:
                    return {"found": False, "message": f"Order {order_id} not found."}
        except Exception as exc:
            log.warning("live_backend_error", method="lookup_order", error=str(exc))

        return self.fallback.lookup_order(order_id, brand_id)

    def track_shipment(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("live_backend_called", method="track_shipment", order_id=order_id, brand_id=brand_id)
        try:
            res = self._make_request(brand_id, "GET", f"/orders/{order_id}/tracking")
            if res and res.status_code == 200:
                return {"tracked": True, **res.json()}
        except Exception as exc:
            log.warning("live_backend_error", method="track_shipment", error=str(exc))

        return self.fallback.track_shipment(order_id, brand_id)

    def initiate_return(
        self,
        order_id: str,
        brand_id: str,
        items_to_return: list[str],
        return_reason: str = "not_specified",
    ) -> dict[str, Any]:
        log.info("live_backend_called", method="initiate_return", order_id=order_id, brand_id=brand_id)
        try:
            res = self._make_request(
                brand_id,
                "POST",
                "/returns",
                json={
                    "order_id": order_id,
                    "brand_id": brand_id,
                    "items": items_to_return,
                    "reason": return_reason,
                }
            )
            if res and res.status_code in (200, 201):
                return res.json()
        except Exception as exc:
            log.warning("live_backend_error", method="initiate_return", error=str(exc))

        return self.fallback.initiate_return(order_id, brand_id, items_to_return, return_reason)

    def check_refund_status(
        self,
        return_ref: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        from core.logging_config import get_active_brand_id
        brand_id = get_active_brand_id() or "boohoo_uk"

        log.info("live_backend_called", method="check_refund_status", return_ref=return_ref, brand_id=brand_id)
        try:
            ref = return_ref or order_id
            res = self._make_request(brand_id, "GET", f"/refunds/{ref}")
            if res and res.status_code == 200:
                return {"found": True, **res.json()}
        except Exception as exc:
            log.warning("live_backend_error", method="check_refund_status", error=str(exc))

        return self.fallback.check_refund_status(return_ref, order_id)

    def cancel_order(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("live_backend_called", method="cancel_order", order_id=order_id, brand_id=brand_id)
        try:
            res = self._make_request(
                brand_id,
                "POST",
                f"/orders/{order_id}/cancel",
                json={"brand_id": brand_id}
            )
            if res and res.status_code == 200:
                return res.json()
        except Exception as exc:
            log.warning("live_backend_error", method="cancel_order", error=str(exc))

        return self.fallback.cancel_order(order_id, brand_id)

    def email_label(
        self,
        order_id: str,
        brand_id: str,
        customer_email: str,
        return_ref: str,
    ) -> dict[str, Any]:
        log.info("live_backend_called", method="email_label", order_id=order_id, return_ref=return_ref, brand_id=brand_id)
        try:
            res = self._make_request(
                brand_id,
                "POST",
                "/email/label",
                json={
                    "order_id": order_id,
                    "brand_id": brand_id,
                    "email": customer_email,
                    "return_ref": return_ref,
                }
            )
            if res and res.status_code in (200, 202):
                return res.json()
        except Exception as exc:
            log.warning("live_backend_error", method="email_label", error=str(exc))

        return self.fallback.email_label(order_id, brand_id, customer_email, return_ref)

    def validate_promo(
        self,
        promo_code: str,
        brand_id: str,
        basket_value: float = 0.0,
    ) -> dict[str, Any]:
        log.info("live_backend_called", method="validate_promo", promo_code=promo_code, brand_id=brand_id)
        try:
            res = self._make_request(
                brand_id,
                "POST",
                "/promos/validate",
                json={
                    "promo_code": promo_code,
                    "brand_id": brand_id,
                    "basket_value": basket_value,
                }
            )
            if res and res.status_code == 200:
                return res.json()
        except Exception as exc:
            log.warning("live_backend_error", method="validate_promo", error=str(exc))

        return self.fallback.validate_promo(promo_code, brand_id, basket_value)

    def search_faqs(self, query: str, brand_id: str) -> dict[str, Any]:
        return self.fallback.search_faqs(query, brand_id)

    def search_policies(self, policy_type: str, brand_id: str) -> dict[str, Any]:
        return self.fallback.search_policies(policy_type, brand_id)

    def get_size_guide(self, product_category: str, brand_id: str) -> dict[str, Any]:
        return self.fallback.get_size_guide(product_category, brand_id)

    def check_account_status(self, customer_id: str, brand_id: str) -> dict[str, Any]:
        log.info("live_backend_called", method="check_account_status", customer_id=customer_id, brand_id=brand_id)
        try:
            res = self._make_request(
                brand_id,
                "GET",
                f"/customers/{customer_id}/status"
            )
            if res and res.status_code == 200:
                return res.json()
        except Exception as exc:
            log.warning("live_backend_error", method="check_account_status", error=str(exc))

        return self.fallback.check_account_status(customer_id, brand_id)

    def escalate_session(
        self,
        session_id: str,
        brand_id: str,
        reason: str,
        priority: str = "P2",
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        log.info("live_backend_called", method="escalate_session", session_id=session_id)
        return self.fallback.escalate_session(session_id, brand_id, reason, priority, customer_id)
