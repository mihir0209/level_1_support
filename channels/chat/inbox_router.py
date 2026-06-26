"""
Chatwoot Inbox Router — maps Chatwoot inbox_id to canonical brand_id.
Supports environment overrides (JSON mapping), multiple inboxes, and registry fallbacks.
"""

from __future__ import annotations

import os
import json
import structlog
from brands.registry import get_brand_by_chatwoot_inbox, BRAND_REGISTRY

log = structlog.get_logger(__name__)

def route_inbox(inbox_id: int | str) -> str:
    """
    Resolve brand_id from a Chatwoot inbox_id.
    
    Precedence:
      1. Environment variable CHATWOOT_INBOX_TO_BRAND (JSON format, e.g. '{"10": "boohoo_uk", "11": "plt_uk"}')
      2. In-memory BRAND_REGISTRY mapping (using chatwoot_inbox_id)
      3. Fallback to default brand (first brand in registry, normally "boohoo_uk")
    """
    inbox_id_int = int(inbox_id) if isinstance(inbox_id, (int, str)) and str(inbox_id).isdigit() else None
    
    # 1. Check environment variable override
    env_map_str = os.environ.get("CHATWOOT_INBOX_TO_BRAND")
    if env_map_str:
        try:
            env_map = json.loads(env_map_str)
            # Support both string and integer keys in JSON mapping
            brand_id = env_map.get(str(inbox_id)) or env_map.get(inbox_id_int)
            if brand_id:
                log.debug("inbox_routed_via_env", inbox_id=inbox_id, brand_id=brand_id)
                return str(brand_id)
        except Exception as e:
            log.warning("inbox_router_env_parse_failed", error=str(e), env_val=env_map_str)

    # 2. Check brand registry
    if inbox_id_int is not None:
        brand_entry = get_brand_by_chatwoot_inbox(inbox_id_int)
        if brand_entry:
            log.debug("inbox_routed_via_registry", inbox_id=inbox_id_int, brand_id=brand_entry.brand_id)
            return brand_entry.brand_id

    # 3. Fallback to default brand
    default_brand = BRAND_REGISTRY[0].brand_id if BRAND_REGISTRY else "boohoo_uk"
    log.warning("inbox_route_not_found_fallback", inbox_id=inbox_id, fallback_brand=default_brand)
    return default_brand
