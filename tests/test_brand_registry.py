"""Tests for brand registry resolution."""

from brands.registry import resolve_brand, get_brand_by_id, load_brands_from_db


def test_resolve_by_brand_id():
    entry = resolve_brand("boohoo_uk")
    assert entry is not None
    assert entry.brand_id == "boohoo_uk"
    assert entry.name == "Boohoo"


def test_resolve_by_domain():
    entry = resolve_brand("prettylittlething.com")
    assert entry is not None
    assert entry.brand_id == "plt_uk"


def test_resolve_nastygal_us():
    entry = resolve_brand("nastygal_us")
    assert entry is not None
    assert entry.default_region == "USA"


def test_resolve_unknown():
    entry = resolve_brand("unknown-brand.com")
    assert entry is None


def test_all_13_brands_registered():
    brand_ids = [
        "boohoo_uk", "boohooman_uk", "plt_uk", "nastygal_us",
        "karenmillen_uk", "dorothyperkins_uk", "wallis_uk", "debenhams_uk",
        "oasis_uk", "warehouse_uk", "coast_uk", "misspap_uk", "burton_uk",
    ]
    for bid in brand_ids:
        entry = get_brand_by_id(bid)
        assert entry is not None, f"Brand {bid} not found in registry"


def test_plt_free_returns_loyalty_note():
    """PLT Royalty members get free returns — verify brand is registered correctly."""
    entry = resolve_brand("plt_uk")
    assert entry is not None
    assert entry.brand_id == "plt_uk"


def test_load_brands_from_db(monkeypatch):
    """Verify that load_brands_from_db loads brand information from PostgreSQL."""
    import json
    from unittest.mock import MagicMock

    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = [True]  # Table exists
    # Return one brand row: brand_id, name, domain, timezone, locale, brand_voice, config
    mock_cursor.fetchall.return_value = [
        ("dynamic_brand_1", "Dynamic Brand 1", "dynamic1.com", "UTC", "en_US", "Friendly tone", json.dumps({
            "domains": ["dynamic1.com", "dynamic1_id"],
            "locales": ["en_US"],
            "default_region": "USA",
            "chatwoot_inbox_id": 42,
            "industry_domain": "telecom"
        }))
    ]

    class CursorContextManagerMock:
        def __enter__(self):
            return mock_cursor
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = CursorContextManagerMock()

    class SyncContextManagerMock:
        def __enter__(self):
            return mock_conn
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_pool = MagicMock()
    mock_pool.connection.return_value = SyncContextManagerMock()

    monkeypatch.setattr("core.db.pool.get_sync_pool", lambda: mock_pool)

    # Load from db
    load_brands_from_db()

    # Resolve
    entry = resolve_brand("dynamic_brand_1")
    assert entry is not None
    assert entry.brand_id == "dynamic_brand_1"
    assert entry.name == "Dynamic Brand 1"
    assert "dynamic1.com" in entry.domains
    assert entry.chatwoot_inbox_id == 42
    assert entry.domain == "telecom"


def test_extract_brand_id_helper():
    """Verify that extract_brand_id dynamically extracts brand_id from request."""
    from fastapi import Request
    from unittest.mock import MagicMock
    from main import extract_brand_id

    # 1. Test query param
    req = MagicMock(spec=Request)
    req.query_params = {"brand_id": "boohoo_uk"}
    req.headers = {}
    req.url = MagicMock()
    req.url.path = "/health"
    req.url.hostname = None
    assert extract_brand_id(req) == "boohoo_uk"

    # 2. Test X-Brand-ID header
    req = MagicMock(spec=Request)
    req.query_params = {}
    req.headers = {"X-Brand-ID": "plt_uk"}
    req.url = MagicMock()
    req.url.path = "/health"
    req.url.hostname = None
    assert extract_brand_id(req) == "plt_uk"

    # 3. Test path segment
    req = MagicMock(spec=Request)
    req.query_params = {}
    req.headers = {}
    req.url = MagicMock()
    req.url.path = "/admin/brands/karenmillen_uk/collect"
    req.url.hostname = None
    assert extract_brand_id(req) == "karenmillen_uk"

    # 4. Test domain hostname
    req = MagicMock(spec=Request)
    req.query_params = {}
    req.headers = {}
    req.url = MagicMock()
    req.url.path = "/health"
    req.url.hostname = "prettylittlething.com"
    assert extract_brand_id(req) == "plt_uk"
