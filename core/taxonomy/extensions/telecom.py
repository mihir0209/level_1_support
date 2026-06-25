"""
Telecom domain taxonomy extension — skeleton for future use.

Maps telecom-specific issue types to base SupportCategory taxonomy.

This demonstrates how the two-layer taxonomy works:
add a new domain = add one file + one YAML config.
No changes needed to core/taxonomy/base.py or orchestration.

IssueType slug → SupportCategory mapping:
  network_outage     → FAULT
  slow_speed         → FAULT
  billing_dispute     → BILLING
  plan_change        → BILLING
  device_issue        → FAULT
  sim_replacement     → SERVICE_STATUS
  number_porting      → SERVICE_STATUS
  account_locked      → ACCOUNT
  contract_query      → INFORMATION
  info                → INFORMATION
  unknown             → INFORMATION
"""

from __future__ import annotations

from core.taxonomy.base import (
    BaseTaxonomyMapper,
    SupportCategory,
)


class TelecomTaxonomyMapper(BaseTaxonomyMapper):
    """
    Telecom domain taxonomy mapper.
    Used by all brands where domain='telecom'.
    """

    DOMAIN = "telecom"

    ISSUE_TYPES = [
        "network_outage",
        "slow_speed",
        "billing_dispute",
        "plan_change",
        "device_issue",
        "sim_replacement",
        "number_porting",
        "account_locked",
        "contract_query",
        "info",
        "unknown",
    ]

    ISSUE_TYPE_TO_CATEGORY: dict[str, SupportCategory] = {
        # FAULT
        "network_outage":  SupportCategory.FAULT,
        "slow_speed":      SupportCategory.FAULT,
        "device_issue":    SupportCategory.FAULT,
        # BILLING
        "billing_dispute": SupportCategory.BILLING,
        "plan_change":      SupportCategory.BILLING,
        # SERVICE_STATUS
        "sim_replacement": SupportCategory.SERVICE_STATUS,
        "number_porting":  SupportCategory.SERVICE_STATUS,
        # ACCOUNT
        "account_locked":  SupportCategory.ACCOUNT,
        # INFORMATION
        "contract_query":  SupportCategory.INFORMATION,
        "info":            SupportCategory.INFORMATION,
        "unknown":         SupportCategory.INFORMATION,
    }

    CATEGORY_TO_ISSUE_TYPES: dict[SupportCategory, list[str]] = {
        cat: [] for cat in SupportCategory
    }
    for issue_type, category in ISSUE_TYPE_TO_CATEGORY.items():
        CATEGORY_TO_ISSUE_TYPES[category].append(issue_type)
