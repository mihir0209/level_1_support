import os
import httpx
import structlog
from typing import Any

from core.tools.backends.interface import ToolBackend
from core.tools.backends.mock_backend import MockBackend

log = structlog.get_logger(__name__)

class LiveBackend(ToolBackend):
    """
    Live API backend routing requests to external microservices (OMS, CRM, etc.).
    Uses configurations from environment variables.
    Falls back to MockBackend logic if live calls fail or are unconfigured.
    """

    def __init__(self):
        self.oms_url = os.environ.get("OMS_API_URL", "")
        self.crm_url = os.environ.get("CRM_API_URL", "")
        self.api_key = os.environ.get("EXTERNAL_API_KEY", "")
        self.fallback = MockBackend()

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def lookup_order(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("live_backend_called", method="lookup_order", order_id=order_id, brand_id=brand_id)
        if not self.oms_url:
            return self.fallback.lookup_order(order_id, brand_id)

        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.get(
                    f"{self.oms_url}/orders/{order_id}",
                    params={"brand_id": brand_id},
                    headers=self._get_headers()
                )
                if res.status_code == 200:
                    return {"found": True, **res.json()}
                elif res.status_code == 404:
                    return {"found": False, "message": f"Order {order_id} not found."}
        except Exception as exc:
            log.warning("live_backend_error", method="lookup_order", error=str(exc))

        return self.fallback.lookup_order(order_id, brand_id)

    def track_shipment(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("live_backend_called", method="track_shipment", order_id=order_id)
        if not self.oms_url:
            return self.fallback.track_shipment(order_id, brand_id)

        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.get(
                    f"{self.oms_url}/orders/{order_id}/tracking",
                    params={"brand_id": brand_id},
                    headers=self._get_headers()
                )
                if res.status_code == 200:
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
        if not self.oms_url:
            return self.fallback.initiate_return(order_id, brand_id, items_to_return, return_reason)

        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.post(
                    f"{self.oms_url}/returns",
                    json={
                        "order_id": order_id,
                        "brand_id": brand_id,
                        "items": items_to_return,
                        "reason": return_reason,
                    },
                    headers=self._get_headers()
                )
                if res.status_code in (200, 201):
                    return res.json()
        except Exception as exc:
            log.warning("live_backend_error", method="initiate_return", error=str(exc))

        return self.fallback.initiate_return(order_id, brand_id, items_to_return, return_reason)

    def check_refund_status(
        self,
        return_ref: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        log.info("live_backend_called", method="check_refund_status", return_ref=return_ref)
        if not self.oms_url:
            return self.fallback.check_refund_status(return_ref, order_id)

        try:
            with httpx.Client(timeout=5.0) as client:
                ref = return_ref or order_id
                res = client.get(
                    f"{self.oms_url}/refunds/{ref}",
                    headers=self._get_headers()
                )
                if res.status_code == 200:
                    return {"found": True, **res.json()}
        except Exception as exc:
            log.warning("live_backend_error", method="check_refund_status", error=str(exc))

        return self.fallback.check_refund_status(return_ref, order_id)

    def cancel_order(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("live_backend_called", method="cancel_order", order_id=order_id)
        if not self.oms_url:
            return self.fallback.cancel_order(order_id, brand_id)

        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.post(
                    f"{self.oms_url}/orders/{order_id}/cancel",
                    json={"brand_id": brand_id},
                    headers=self._get_headers()
                )
                if res.status_code == 200:
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
        log.info("live_backend_called", method="email_label", order_id=order_id, return_ref=return_ref)
        if not self.crm_url:
            return self.fallback.email_label(order_id, brand_id, customer_email, return_ref)

        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.post(
                    f"{self.crm_url}/email/label",
                    json={
                        "order_id": order_id,
                        "brand_id": brand_id,
                        "email": customer_email,
                        "return_ref": return_ref,
                    },
                    headers=self._get_headers()
                )
                if res.status_code in (200, 202):
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
        if not self.oms_url:
            return self.fallback.validate_promo(promo_code, brand_id, basket_value)

        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.post(
                    f"{self.oms_url}/promos/validate",
                    json={
                        "promo_code": promo_code,
                        "brand_id": brand_id,
                        "basket_value": basket_value,
                    },
                    headers=self._get_headers()
                )
                if res.status_code == 200:
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
        log.info("live_backend_called", method="check_account_status", customer_id=customer_id)
        if not self.crm_url:
            return self.fallback.check_account_status(customer_id, brand_id)

        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.get(
                    f"{self.crm_url}/customers/{customer_id}/status",
                    params={"brand_id": brand_id},
                    headers=self._get_headers()
                )
                if res.status_code == 200:
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
        # Typically Chatwoot webhook trigger/creation
        return self.fallback.escalate_session(session_id, brand_id, reason, priority, customer_id)
