"""
Domain configuration loader.

Loads domain YAML configs from the domains/ directory and provides
a typed interface for accessing domain-specific settings.

Usage:
    from domains.loader import load_domain_config, get_domain_config

    cfg = get_domain_config("ecommerce")
    tools = cfg.tool_manifest["wismo"]          # list of tool names
    specialist = cfg.issue_types["refund_delay"].specialist  # agent ID
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

__all__ = [
    "load_domain_config",
    "get_domain_config",
    "get_all_domain_ids",
    "DomainConfig",
    "IssueTypeSpec",
    "SpecialistSpec",
]


# ── Pydantic models for type-safe config access ────────────────────────────────

class IssueTypeSpec(BaseModel):
    name: str
    description: str
    category: str
    avg_resolution_time_seconds: int = 60
    specialist: str
    # Allow extra fields (future expansion) without breaking
    model_config = BaseModel.model_config | {"extra": "allow"}


class ToolManifest(BaseModel):
    model_config = BaseModel.model_config | {"extra": "allow"}


class SpecialistSpec(BaseModel):
    name: str
    description: str
    system_prompt_template: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    model_config = BaseModel.model_config | {"extra": "allow"}


class BrandTierSpec(BaseModel):
    display_name: str
    return_cost_gbp: float | None = None
    return_cost_usd: float | None = None
    return_window_days: int | None = None
    model_config = BaseModel.model_config | {"extra": "allow"}


class DomainConfig(BaseModel):
    """
    Fully-typed representation of a domain YAML config.
    All fields are optional so that partial configs don't break — missing
    keys fall back to the default e-commerce config or return None.
    """
    domain_id: str = "ecommerce"
    domain_name: str = "E-Commerce"
    description: str = ""

    # Ordered list of defined IssueType slugs
    issue_type_ids: list[str] = Field(default_factory=list)

    # issue_type_id → IssueTypeSpec
    issue_types: dict[str, IssueTypeSpec] = Field(default_factory=dict)

    # category mapping: issue_type_id → base category string
    category_mapping: dict[str, str] = Field(default_factory=dict)

    # issue_type_id → list of tool names
    tool_manifest: dict[str, list[str]] = Field(default_factory=dict)

    # specialist_id → SpecialistSpec
    specialist_agents: dict[str, SpecialistSpec] = Field(default_factory=dict)

    # brand tier → display/return rules
    brand_tiers: dict[str, BrandTierSpec] = Field(default_factory=dict)

    # list of excluded item category strings
    excluded_item_categories: list[str] = Field(default_factory=list)

    # Extra raw fields (YAML anchors, comments, etc.)
    model_config = BaseModel.model_config | {"extra": "allow"}

    def get_specialist_for(self, issue_type_id: str) -> str | None:
        """Return the specialist agent ID for a given IssueType."""
        spec = self.issue_types.get(issue_type_id)
        return spec.specialist if spec else None

    def get_tools_for(self, issue_type_id: str) -> list[str]:
        """Return the list of tool names for a given IssueType."""
        return self.tool_manifest.get(issue_type_id, [])

    def get_category_for(self, issue_type_id: str) -> str | None:
        """Return the base SupportCategory string for an IssueType."""
        return self.category_mapping.get(issue_type_id)

    def get_tier_display(self, tier: str) -> str:
        """Return display name for a brand tier."""
        return self.brand_tiers.get(tier, BrandTierSpec(display_name=tier)).display_name


# ── Config registry ───────────────────────────────────────────────────────────

# Lazily loaded domain configs keyed by domain_id
_DOMAIN_CONFIGS: dict[str, DomainConfig] = {}

# Path to domains/ directory (relative to project root)
_DOMAINS_DIR = Path(__file__).parent.parent / "domains"


def _load_yaml(domain_id: str) -> dict[str, Any]:
    """Load and parse a domain YAML file."""
    path = _DOMAINS_DIR / domain_id / "config.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Domain config not found: {path}")

    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def load_domain_config(domain_id: str) -> DomainConfig:
    """
    Load and parse a domain YAML config into a typed DomainConfig.

    Builds structured IssueTypeSpec and SpecialistSpec objects from
    the flat YAML lists. Caches the result on first load.
    """
    if domain_id in _DOMAIN_CONFIGS:
        return _DOMAIN_CONFIGS[domain_id]

    raw = _load_yaml(domain_id)

    # Build structured issue type dict
    issue_types: dict[str, IssueTypeSpec] = {}
    issue_type_ids: list[str] = []
    for it_id, it_raw in raw.get("issue_types", {}).items():
        issue_types[it_id] = IssueTypeSpec(**it_raw)
        issue_type_ids.append(it_id)

    # Build structured specialist dict
    specialists: dict[str, SpecialistSpec] = {}
    for sp_id, sp_raw in raw.get("specialist_agents", {}).items():
        specialists[sp_id] = SpecialistSpec(**sp_raw)

    # Build tier dict
    tiers: dict[str, BrandTierSpec] = {}
    for tier_id, tier_raw in raw.get("brand_tiers", {}).items():
        tiers[tier_id] = BrandTierSpec(**tier_raw)

    cfg = DomainConfig(
        domain_id=raw.get("domain_id", domain_id),
        domain_name=raw.get("domain_name", domain_id),
        description=raw.get("description", ""),
        issue_type_ids=issue_type_ids,
        issue_types=issue_types,
        category_mapping=raw.get("category_mapping", {}),
        tool_manifest=raw.get("tool_manifest", {}),
        specialist_agents=specialists,
        brand_tiers=tiers,
        excluded_item_categories=raw.get("excluded_item_categories", []),
    )

    _DOMAIN_CONFIGS[domain_id] = cfg
    return cfg


def get_domain_config(domain_id: str) -> DomainConfig:
    """
    Get a domain config, falling back to "ecommerce" if not found.
    This prevents crashes when a brand doesn't have a domain-specific config yet.
    """
    try:
        return load_domain_config(domain_id)
    except FileNotFoundError:
        import warnings
        warnings.warn(
            f"Domain config for '{domain_id}' not found, falling back to 'ecommerce'",
            UserWarning,
            stacklevel=2,
        )
        return load_domain_config("ecommerce")


def get_all_domain_ids() -> list[str]:
    """List all available domain IDs (directory names in domains/)."""
    if not _DOMAINS_DIR.exists():
        return ["ecommerce"]
    return [
        d.name for d in _DOMAINS_DIR.iterdir()
        if d.is_dir() and (d / "config.yaml").exists()
    ]


def clear_domain_cache() -> None:
    """Clear the loaded config cache. Useful in tests."""
    _DOMAIN_CONFIGS.clear()