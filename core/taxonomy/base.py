"""
Industry-agnostic base taxonomy — the universal layer every domain shares.

This is NOT a flat enum. It defines the structural categories that any
L1 support system needs regardless of industry:

  - ServiceStatus: what state is the customer's service/product in?
  - AccountStanding: what state is the customer's account in?
  - SupportCategory: the high-level reason for contact
  - InteractionChannel: how did the customer reach us?
  - ResolutionOutcome: what happened at the end?
  - EscalationTier: how far did this escalate?

Each industry extends these with domain-specific IssueTypes and
mappings via core/taxonomy/extensions/{domain}.py
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.ontology.models import IntentResult


# ══════════════════════════════════════════════════════════════════════════════
# BASE SUPPORT CATEGORIES  (industry-agnostic)
# ══════════════════════════════════════════════════════════════════════════════

class SupportCategory(StrEnum):
    """
    Universal high-level contact reasons.
    Every industry maps its own IssueTypes to one of these.
    """
    SERVICE_STATUS = "service_status"   # delivery progress, outage, plan activation
    BILLING = "billing"                 # payment, refund, charge dispute, promo
    ACCOUNT = "account"                # login, access, profile, cancellation
    FAULT = "fault"                     # product fault, wrong item, service error
    INFORMATION = "information"         # general query, policy question
    ENQUIRY = "enquiry"                 # availability, sizing, plan features


class ServiceStatus(StrEnum):
    """
    Possible states of a customer's service, order, or subscription.
    E-commerce: order lifecycle
    Telecom: network/service status
    Healthcare: appointment/subscription status
    SaaS: license/access status
    """
    PENDING = "pending"                 # not yet processed
    ACTIVE = "active"                  # in good standing
    IN_PROGRESS = "in_progress"         # being worked on
    DISRUPTED = "disrupted"             # delayed / degraded / partial issue
    SUSPENDED = "suspended"             # paused / temporarily stopped
    CANCELLED = "cancelled"            # terminated / refunded
    COMPLETED = "completed"             # delivered / resolved / fulfilled
    FAILED = "failed"                  # error state


class AccountStanding(StrEnum):
    """
    State of the customer's account relationship.
    """
    ACTIVE = "active"
    RESTRICTED = "restricted"           # limited access (payment hold, fraud review)
    SUSPENDED = "suspended"             # temporarily locked
    CLOSED = "closed"                   # cancelled / churned
    PENDING_VERIFICATION = "pending_verification"


class ResolutionOutcome(StrEnum):
    """
    Session-level outcome.
    """
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"
    DEFERRED = "deferred"               # follow-up promised


class EscalationTier(StrEnum):
    """
    How far the issue escalated.
    """
    L1 = "L1"                           # AI agent
    L2 = "L2"                           # human L1 agent
    L3 = "L3"                           # specialist / manager
    L4 = "L4"                           # legal / executive


class InteractionChannel(StrEnum):
    """
    How the customer contacted support.
    """
    CHAT = "chat"
    EMAIL = "email"
    VOICE = "voice"
    WHATSAPP = "whatsapp"
    SOCIAL = "social"
    PORTAL = "portal"                   # in-app / self-service portal


# ══════════════════════════════════════════════════════════════════════════════
# BASE CUSTOMER TIERS  (universal loyalty concept)
# ══════════════════════════════════════════════════════════════════════════════

class CustomerTier(StrEnum):
    """
    Customer value tier. Names are brand-defined;
    the tier labels here are universal.
    """
    STANDARD = "standard"
    PREFERRED = "preferred"              # mid-tier loyalty (was "LOYALTY")
    VIP = "vip"                         # top-tier


# ══════════════════════════════════════════════════════════════════════════════
# MAPPING INTERFACE — each domain extension implements this
# ══════════════════════════════════════════════════════════════════════════════

class BaseTaxonomyMapper:
    """
    Interface for mapping domain-specific IssueTypes to base categories
    and vice versa. Every domain extension implements this.

    Example (e-commerce):
        iss
```
uetype → SupportCategory
        "wismo"         → SupportCategory.SERVICE_STATUS
        "return_initiate" → SupportCategory.BILLING
        "account_locked"  → SupportCategory.ACCOUNT

    Example (telecom):
        "network_outage"  → SupportCategory.FAULT
        "billing_dispute" → SupportCategory.BILLING
        "number_porting"  → SupportCategory.SERVICE_STATUS
    ```
    """

    # Domain identifier used in KG queries and session routing
    DOMAIN: str = "base"

    # Ordered list of domain-specific IssueType IDs for this domain
    ISSUE_TYPES: list[str] = []

    # Map: domain IssueType slug → base SupportCategory
    ISSUE_TYPE_TO_CATEGORY: dict[str, SupportCategory] = {}

    # Map: base SupportCategory → ordered list of domain IssueType slugs
    CATEGORY_TO_ISSUE_TYPES: dict[SupportCategory, list[str]] = {}

    @classmethod
    def get_category_for(cls, issue_type_id: str) -> SupportCategory:
        """Map a domain IssueType to its base SupportCategory."""
        return cls.ISSUE_TYPE_TO_CATEGORY.get(
            issue_type_id, SupportCategory.INFORMATION
        )

    @classmethod
    def get_issue_types_for(cls, category: SupportCategory) -> list[str]:
        """Get all domain IssueTypes that belong to a base category."""
        return cls.CATEGORY_TO_ISSUE_TYPES.get(category, [])

    @classmethod
    def is_known_issue_type(cls, issue_type_id: str) -> bool:
        """Check if this IssueType is defined for the domain."""
        return issue_type_id in cls.ISSUE_TYPES
