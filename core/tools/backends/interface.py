from abc import ABC, abstractmethod
from typing import Any

class ToolBackend(ABC):
    """
    Abstract interface for all tool execution backends.
    Allows routing tool actions to mock logic, JSON stub fixtures, or live external APIs.
    """

    @abstractmethod
    def lookup_order(self, order_id: str, brand_id: str) -> dict[str, Any]:
        """Look up the status and details of a customer's order."""
        pass

    @abstractmethod
    def track_shipment(self, order_id: str, brand_id: str) -> dict[str, Any]:
        """Retrieve detailed shipment tracking information for an order."""
        pass

    @abstractmethod
    def initiate_return(
        self,
        order_id: str,
        brand_id: str,
        items_to_return: list[str],
        return_reason: str = "not_specified",
    ) -> dict[str, Any]:
        """Initiate a return for one or more items from an order."""
        pass

    @abstractmethod
    def check_refund_status(
        self,
        return_ref: str | None = None,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        """Check the status of a refund for a given return reference or order ID."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str, brand_id: str) -> dict[str, Any]:
        """Attempt to cancel an order."""
        pass

    @abstractmethod
    def email_label(
        self,
        order_id: str,
        brand_id: str,
        customer_email: str,
        return_ref: str,
    ) -> dict[str, Any]:
        """Send a returns label to the customer's email address."""
        pass

    @abstractmethod
    def validate_promo(
        self,
        promo_code: str,
        brand_id: str,
        basket_value: float = 0.0,
    ) -> dict[str, Any]:
        """Validate whether a promotional/discount code is active and applicable."""
        pass

    @abstractmethod
    def search_faqs(self, query: str, brand_id: str) -> dict[str, Any]:
        """Search the brand's FAQ knowledge base."""
        pass

    @abstractmethod
    def search_policies(self, policy_type: str, brand_id: str) -> dict[str, Any]:
        """Retrieve the brand's policy for a specific topic."""
        pass

    @abstractmethod
    def get_size_guide(self, product_category: str, brand_id: str) -> dict[str, Any]:
        """Retrieve size guide information for a product category."""
        pass

    @abstractmethod
    def check_account_status(self, customer_id: str, brand_id: str) -> dict[str, Any]:
        """Check if a customer's account is active, suspended, or locked."""
        pass

    @abstractmethod
    def escalate_session(
        self,
        session_id: str,
        brand_id: str,
        reason: str,
        priority: str = "P2",
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        """Escalate the session to a human agent by creating a support ticket."""
        pass
