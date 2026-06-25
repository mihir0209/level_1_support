import json
import uuid
import structlog
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta, timezone

from core.tools.backends.interface import ToolBackend
from core.tools.backends.mock_backend import MockBackend

log = structlog.get_logger(__name__)

class StubBackend(ToolBackend):
    """
    Stub backend that reads from configurable JSON fixtures in data/fixtures/.
    Falls back to MockBackend logic if fixtures are missing or keys are not found.
    """

    def __init__(self, fixtures_dir: Path | None = None):
        if fixtures_dir is None:
            # First try project_root/fixtures, then project_root/data/fixtures
            root = Path(__file__).parent.parent.parent.parent
            cand1 = root / "fixtures"
            cand2 = root / "data" / "fixtures"
            if cand1.exists():
                self.fixtures_dir = cand1
            else:
                self.fixtures_dir = cand2
        else:
            self.fixtures_dir = fixtures_dir

        self.mock_fallback = MockBackend()
        self._MOCK_RETURNS = {}

    def _load_fixture(self, filename: str) -> dict[str, Any]:
        filepath = self.fixtures_dir / filename
        if not filepath.exists():
            log.warning("fixture_file_missing", path=str(filepath))
            return {}
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except Exception as exc:
            log.error("fixture_load_failed", path=str(filepath), error=str(exc))
            return {}

    def lookup_order(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("stub_backend_called", method="lookup_order", order_id=order_id, brand_id=brand_id)
        orders = self._load_fixture("orders.json")
        normalized = order_id.upper().strip()

        order = orders.get(normalized)
        if not order:
            # Fall back to mock backend
            return self.mock_fallback.lookup_order(order_id, brand_id)

        if order.get("brand_id") != brand_id:
            return {
                "found": False,
                "message": f"Order {order_id} was not found for this brand. Please check you're contacting the correct brand's support.",
            }

        return {"found": True, **order}

    def track_shipment(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("stub_backend_called", method="track_shipment", order_id=order_id)
        tracking_data = self._load_fixture("tracking.json")
        normalized = order_id.upper().strip()

        shipment = tracking_data.get(normalized)
        if not shipment:
            # Fall back to mock order's shipment, then mock backend directly
            order_res = self.lookup_order(order_id, brand_id)
            if order_res.get("found") and order_res.get("shipment"):
                return {"tracked": True, **order_res["shipment"]}
            return self.mock_fallback.track_shipment(order_id, brand_id)

        return {"tracked": True, **shipment}

    def initiate_return(
        self,
        order_id: str,
        brand_id: str,
        items_to_return: list[str],
        return_reason: str = "not_specified",
    ) -> dict[str, Any]:
        log.info("stub_backend_called", method="initiate_return", order_id=order_id, brand_id=brand_id)
        # Initiating returns writes to our active local returns dictionary
        return_ref = f"RET-{uuid.uuid4().hex[:8].upper()}"
        self._MOCK_RETURNS[return_ref] = {
            "return_ref": return_ref,
            "order_id": order_id,
            "brand_id": brand_id,
            "items": items_to_return,
            "reason": return_reason,
            "status": "initiated",
            "initiated_at": datetime.now(timezone.utc).isoformat(),
            "refund_eta": (datetime.now(timezone.utc) + timedelta(days=21)).isoformat(),
        }
        return {
            "success": True,
            "return_ref": return_ref,
            "message": (
                f"Return initiated. Reference: {return_ref}. "
                "Please check your email for your returns label. "
                "Refund will be processed within 21 days of us receiving the parcel."
            ),
            "refund_eta_days": 21,
        }

    def check_refund_status(
        self,
        return_ref: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        log.info("stub_backend_called", method="check_refund_status", return_ref=return_ref, order_id=order_id)
        if return_ref and return_ref in self._MOCK_RETURNS:
            ret = self._MOCK_RETURNS[return_ref]
            initiated_at = datetime.fromisoformat(ret["initiated_at"])
            days_elapsed = (datetime.now(timezone.utc) - initiated_at).days
            return {
                "found": True,
                "return_ref": return_ref,
                "status": ret["status"],
                "days_elapsed": days_elapsed,
                "days_remaining": max(0, 21 - days_elapsed),
                "refund_eta": ret["refund_eta"],
                "overdue": days_elapsed > 21,
            }

        # Check in refund fixtures if any exist
        refunds = self._load_fixture("refunds.json")
        if return_ref and return_ref in refunds:
            return {"found": True, **refunds[return_ref]}

        # Fallback
        return self.mock_fallback.check_refund_status(return_ref, order_id)

    def cancel_order(self, order_id: str, brand_id: str) -> dict[str, Any]:
        log.info("stub_backend_called", method="cancel_order", order_id=order_id)
        # Note: Cancellation in stub writes dynamically to mock fallback or allows it
        return self.mock_fallback.cancel_order(order_id, brand_id)

    def email_label(
        self,
        order_id: str,
        brand_id: str,
        customer_email: str,
        return_ref: str,
    ) -> dict[str, Any]:
        log.info("stub_backend_called", method="email_label", order_id=order_id, return_ref=return_ref)
        return {
            "sent": True,
            "message": f"Returns label sent to {customer_email}. Please allow a few minutes for delivery.",
            "return_ref": return_ref,
        }

    def validate_promo(
        self,
        promo_code: str,
        brand_id: str,
        basket_value: float = 0.0,
    ) -> dict[str, Any]:
        log.info("stub_backend_called", method="validate_promo", promo_code=promo_code, brand_id=brand_id)
        promos = self._load_fixture("promos.json")
        promo = promos.get(promo_code.upper())

        if not promo:
            return self.mock_fallback.validate_promo(promo_code, brand_id, basket_value)

        if not promo.get("active", True):
            return {"valid": False, "reason": "This promotional code has expired."}
        if promo.get("applies_to_brands") and brand_id not in promo["applies_to_brands"]:
            return {"valid": False, "reason": "This code is not valid for this brand."}
        min_spend = promo.get("min_spend", 0.0)
        if basket_value < min_spend:
            return {
                "valid": False,
                "reason": f"Minimum spend of £{min_spend:.2f} required. Add more items to qualify.",
            }

        return {
            "valid": True,
            "discount_pct": promo.get("discount_pct", 10),
            "message": f"Code is valid. {promo.get('discount_pct', 10)}% discount will be applied at checkout.",
        }

    def search_faqs(self, query: str, brand_id: str) -> dict[str, Any]:
        log.info("stub_backend_called", method="search_faqs", query=query, brand_id=brand_id)
        # FAQs are typically search/retriever-driven, but stub can load faqs.json if available
        faqs = self._load_fixture("faqs.json")
        brand_faqs = faqs.get(brand_id)
        if brand_faqs and query:
            import re
            query_lower = query.lower()
            scored = []
            for faq in brand_faqs:
                q_words = set(re.findall(r'\w+', faq.get("question", "").lower()))
                a_words = set(re.findall(r'\w+', faq.get("answer", "").lower()))
                combined = q_words | a_words
                score = sum(1 for w in query_lower.split() if w in combined)
                scored.append((score, faq))
            scored.sort(key=lambda x: x[0], reverse=True)
            results = [{"question": f["question"], "answer": f["answer"], "score": s} for s, f in scored[:3] if s > 0]
            if results:
                faqs_text = "\n\n".join(f"Q: {r['question']}\nA: {r['answer']}" for r in results)
                return {"found": True, "results": results, "answer": faqs_text}

        return self.mock_fallback.search_faqs(query, brand_id)

    def search_policies(self, policy_type: str, brand_id: str) -> dict[str, Any]:
        log.info("stub_backend_called", method="search_policies", policy_type=policy_type, brand_id=brand_id)
        policies = self._load_fixture("policies.json")
        brand_policies = policies.get(brand_id)
        if brand_policies and policy_type in brand_policies:
            return {"found": True, "policy_type": policy_type, "policy": brand_policies[policy_type]}

        return self.mock_fallback.search_policies(policy_type, brand_id)

    def get_size_guide(self, product_category: str, brand_id: str) -> dict[str, Any]:
        log.info("stub_backend_called", method="get_size_guide", category=product_category, brand_id=brand_id)
        sizes = self._load_fixture("sizes.json")
        if product_category in sizes:
            return {
                "category": product_category,
                "brand_id": brand_id,
                "size_guide": sizes[product_category],
            }

        return self.mock_fallback.get_size_guide(product_category, brand_id)

    def check_account_status(self, customer_id: str, brand_id: str) -> dict[str, Any]:
        log.info("stub_backend_called", method="check_account_status", customer_id=customer_id, brand_id=brand_id)
        accounts = self._load_fixture("accounts.json")
        account = accounts.get(customer_id)
        if account:
            return {"customer_id": customer_id, **account}

        return self.mock_fallback.check_account_status(customer_id, brand_id)

    def escalate_session(
        self,
        session_id: str,
        brand_id: str,
        reason: str,
        priority: str = "P2",
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        log.info("stub_backend_called", method="escalate_session", session_id=session_id, reason=reason, priority=priority)
        return self.mock_fallback.escalate_session(session_id, brand_id, reason, priority, customer_id)
