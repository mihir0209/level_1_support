"""
Taxonomy system — two-layer (base + domain extension).

Usage:
    from core.taxonomy import get_mapper, resolve_intent_category

    mapper = get_mapper("ecommerce")
    cat = mapper.get_category_for("return_initiate")   # SupportCategory.BILLING
    its = mapper.get_issue_types_for(SupportCategory.BILLING)  # ["return_initiate", "refund_delay", ...]
"""

from __future__ import annotations

from core.taxonomy.base import (
    SupportCategory,
    ServiceStatus,
    AccountStanding,
    ResolutionOutcome,
    EscalationTier,
    InteractionChannel,
    CustomerTier,
    BaseTaxonomyMapper,
)
from core.taxonomy.extensions.ecommerce import EcommerceTaxonomyMapper
from core.taxonomy.extensions.telecom import TelecomTaxonomyMapper

__all__ = [
    "SupportCategory",
    "ServiceStatus",
    "AccountStanding",
    "ResolutionOutcome",
    "EscalationTier",
    "InteractionChannel",
    "CustomerTier",
    "BaseTaxonomyMapper",
    "EcommerceTaxonomyMapper",
    "TelecomTaxonomyMapper",
    "get_mapper",
    "get_all_domains",
    "resolve_intent_category",
]


# ── Domain registry ───────────────────────────────────────────────────────────

_DOMAIN_MAPPERS: dict[str, type[BaseTaxonomyMapper]] = {
    "ecommerce": EcommerceTaxonomyMapper,
    "telecom": TelecomTaxonomyMapper,
}


def get_mapper(domain: str) -> BaseTaxonomyMapper:
    """
    Return the taxonomy mapper for a domain.

    Defaults to EcommerceTaxonomyMapper if domain is unknown —
    existing brands won't break if they don't have a domain tag yet.
    """
    mapper_cls = _DOMAIN_MAPPERS.get(domain.lower(), EcommerceTaxonomyMapper)
    return mapper_cls()


def get_all_domains() -> list[str]:
    """List all registered domain identifiers."""
    return list(_DOMAIN_MAPPERS.keys())


def register_domain(domain: str, mapper_cls: type[BaseTaxonomyMapper]) -> None:
    """
    Register a new domain taxonomy at runtime.
    Use this when loading domain configs dynamically.
    """
    _DOMAIN_MAPPERS[domain.lower()] = mapper_cls


def resolve_intent_category(
    issue_type_id: str,
    domain: str,
) -> SupportCategory:
    """
    Resolve a domain-specific IssueType to its base SupportCategory.

    This is the canonical lookup used by assemble_context and retriever.
    """
    mapper = get_mapper(domain)
    return mapper.get_category_for(issue_type_id)
