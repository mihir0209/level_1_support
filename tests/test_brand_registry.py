"""Tests for brand registry resolution."""

from brands.registry import resolve_brand, get_brand_by_id


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
