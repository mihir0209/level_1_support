"""
Brand registry — maps inbound identifiers (subdomain, API key, Chatwoot inbox ID)
to canonical brand_id used throughout the system.

When a new brand is onboarded, add one entry here + run seed_data.py.
Everything else (graph queries, system prompts, policies) is data-driven.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BrandEntry:
    brand_id: str
    name: str
    domains: list[str]         # subdomains / hostnames that map to this brand
    locales: list[str]         # e.g. ["en-GB", "en-US"]
    default_region: str        # for shipping policy lookup
    chatwoot_inbox_id: int | None = None
    domain: str = "ecommerce"  # industry domain — determines taxonomy + tool manifest


# All 13 Boohoo Group brands registered
BRAND_REGISTRY: list[BrandEntry] = [
    BrandEntry(
        brand_id="boohoo_uk", name="Boohoo",
        domains=["boohoo.com", "uk.boohoo.com", "boohoo_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="boohooman_uk", name="BoohooMAN",
        domains=["boohooman.com", "uk.boohooman.com", "boohooman_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="plt_uk", name="PrettyLittleThing",
        domains=["prettylittlething.com", "plt.com", "plt_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="nastygal_us", name="Nasty Gal",
        domains=["nastygal.com", "nastygal_us"],
        locales=["en-US"], default_region="USA",
    ),
    BrandEntry(
        brand_id="karenmillen_uk", name="Karen Millen",
        domains=["karenmillen.com", "karenmillen_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="dorothyperkins_uk", name="Dorothy Perkins",
        domains=["dorothyperkins.com", "dorothyperkins_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="wallis_uk", name="Wallis",
        domains=["wallis.co.uk", "wallis_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="debenhams_uk", name="Debenhams",
        domains=["debenhams.com", "debenhams_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="oasis_uk", name="Oasis",
        domains=["oasis-stores.com", "oasis_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="warehouse_uk", name="Warehouse",
        domains=["warehouse.co.uk", "warehouse_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="coast_uk", name="Coast",
        domains=["coast-stores.com", "coast_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="misspap_uk", name="MissPap",
        domains=["misspap.co.uk", "misspap_uk"],
        locales=["en-GB"], default_region="UK",
    ),
    BrandEntry(
        brand_id="burton_uk", name="Burton",
        domains=["burton.co.uk", "burton_uk"],
        locales=["en-GB"], default_region="UK",
    ),
]

# Build lookup indexes
_by_domain: dict[str, BrandEntry] = {}
_by_id: dict[str, BrandEntry] = {}
_by_chatwoot_inbox: dict[int, BrandEntry] = {}
for entry in BRAND_REGISTRY:
    _by_id[entry.brand_id] = entry
    for domain in entry.domains:
        _by_domain[domain.lower()] = entry
    if entry.chatwoot_inbox_id is not None:
        _by_chatwoot_inbox[entry.chatwoot_inbox_id] = entry


def resolve_brand(identifier: str) -> BrandEntry | None:
    """Resolve a brand from any identifier: brand_id, domain, or subdomain."""
    identifier = identifier.lower().strip()
    return _by_id.get(identifier) or _by_domain.get(identifier)


def get_brand_by_id(brand_id: str) -> BrandEntry | None:
    return _by_id.get(brand_id)


def get_brand_by_chatwoot_inbox(inbox_id: int) -> BrandEntry | None:
    """Resolve brand from a Chatwoot inbox ID. Returns None if not found."""
    return _by_chatwoot_inbox.get(inbox_id)


def register_brand(entry: BrandEntry) -> bool:
    """
    Dynamically register a new brand at runtime.
    Adds to the in-memory registry and lookup indexes.
    Returns True if newly added, False if brand_id already existed (no-op).
    """
    if entry.brand_id in _by_id:
        return False
    BRAND_REGISTRY.append(entry)
    _by_id[entry.brand_id] = entry
    for domain in entry.domains:
        _by_domain[domain.lower()] = entry
    if entry.chatwoot_inbox_id is not None:
        _by_chatwoot_inbox[entry.chatwoot_inbox_id] = entry
    return True


def load_brands_from_db() -> None:
    """
    Load all registered brands from PostgreSQL database into memory registry.
    """
    try:
        from core.db.pool import get_sync_pool
        pool = get_sync_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Check if the table exists first (to prevent errors during migrations)
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'public' 
                        AND table_name = 'registered_brands'
                    );
                """)
                exists = cur.fetchone()[0]
                if not exists:
                    return

                cur.execute("""
                    SELECT brand_id, name, domain, timezone, locale, brand_voice, config 
                    FROM registered_brands;
                """)
                rows = cur.fetchall()
                for row in rows:
                    brand_id, name, domain, timezone, locale, brand_voice, config = row
                    if isinstance(config, str):
                        import json
                        config_dict = json.loads(config)
                    else:
                        config_dict = config or {}

                    # Construct BrandEntry
                    domains = config_dict.get("domains") or [domain, brand_id]
                    locales = config_dict.get("locales") or [locale]
                    default_region = config_dict.get("default_region") or "UK"
                    chatwoot_inbox_id = config_dict.get("chatwoot_inbox_id")
                    industry_domain = config_dict.get("industry_domain") or "ecommerce"

                    entry = BrandEntry(
                        brand_id=brand_id,
                        name=name,
                        domains=domains,
                        locales=locales,
                        default_region=default_region,
                        chatwoot_inbox_id=chatwoot_inbox_id,
                        domain=industry_domain,
                    )
                    register_brand(entry)
    except Exception as e:
        import structlog
        structlog.get_logger(__name__).warning("load_brands_from_db_failed", error=str(e))
