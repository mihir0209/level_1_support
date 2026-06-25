"""
E-commerce domain taxonomy extension.

Maps all Boohoo Group issue types to the base SupportCategory taxonomy.
All 13 Boohoo brands use this extension.

IssueType slug → SupportCategory mapping:
  wismo              → SERVICE_STATUS   (where is my order?)
  delivery_query     → SERVICE_STATUS   (general delivery enquiry)
  return_initiate    → BILLING          (start a return / refund flow)
  refund_delay       → BILLING          (chasing a refund)
  wrong_item         → FAULT            (received wrong product)
  damaged_item       → FAULT            (received faulty product)
  order_cancel       → SERVICE_STATUS   (cancel before dispatch)
  payment_failed     → BILLING          (payment declined)
  promo_not_applied  → BILLING          (discount code issue)
  klarna_return      → BILLING          (BNPL-specific return)
  afterpay_return    → BILLING          (BNPL-specific return)
  size_query         → INFORMATION      (sizing help)
  account_locked     → ACCOUNT          (login/access issues)
  info               → INFORMATION      (general policy/FAQ query)
  unknown            → INFORMATION      (unclassified — fallback)
"""

from __future__ import annotations

from core.taxonomy.base import (
    BaseTaxonomyMapper,
    SupportCategory,
)


class EcommerceTaxonomyMapper(BaseTaxonomyMapper):
    """
    E-commerce domain taxonomy mapper.
    Used by all brands where domain='ecommerce'.
    """

    DOMAIN = "ecommerce"

    # All defined IssueType slugs for e-commerce
    ISSUE_TYPES = [
        "wismo",
        "delivery_query",
        "return_initiate",
        "refund_delay",
        "wrong_item",
        "damaged_item",
        "order_cancel",
        "payment_failed",
        "promo_not_applied",
        "klarna_return",
        "afterpay_return",
        "size_query",
        "account_locked",
        "info",
        "unknown",
    ]

    ISSUE_TYPE_TO_CATEGORY: dict[str, SupportCategory] = {
        # SERVICE_STATUS
        "wismo":           SupportCategory.SERVICE_STATUS,
        "delivery_query":  SupportCategory.SERVICE_STATUS,
        "order_cancel":    SupportCategory.SERVICE_STATUS,
        # BILLING
        "return_initiate": SupportCategory.BILLING,
        "refund_delay":    SupportCategory.BILLING,
        "payment_failed":  SupportCategory.BILLING,
        "promo_not_applied": SupportCategory.BILLING,
        "klarna_return":   SupportCategory.BILLING,
        "afterpay_return": SupportCategory.BILLING,
        # FAULT
        "wrong_item":      SupportCategory.FAULT,
        "damaged_item":    SupportCategory.FAULT,
        # ACCOUNT
        "account_locked":  SupportCategory.ACCOUNT,
        # INFORMATION
        "size_query":      SupportCategory.INFORMATION,
        "info":            SupportCategory.INFORMATION,
        "unknown":         SupportCategory.INFORMATION,
    }

    # Reverse mapping: base category → list of domain IssueTypes
    CATEGORY_TO_ISSUE_TYPES: dict[SupportCategory, list[str]] = {
        cat: [] for cat in SupportCategory
    }
    for issue_type, category in ISSUE_TYPE_TO_CATEGORY.items():
        CATEGORY_TO_ISSUE_TYPES[category].append(issue_type)

    # Brand tier display names (used in dynamically generated prompts)
    TIER_DISPLAY_NAMES: dict[str, str] = {
        "premium": "Premium",
        "mid": "Mid-market",
        "budget": "Budget",
    }

    # Non-returnable item categories (abstract — actual excluded list
    # comes from the KG ReturnPolicy node; these are defaults)
    EXCLUDED_ITEM_CATEGORIES: list[str] = [
        "swimwear",
        "underwear",
        "lingerie",
        "pierced jewellery",
        "beauty products",
    ]
