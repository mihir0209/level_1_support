"""
Core fetch module.

Provides unified web fetching with multiple backends:
- Obscura (Rust headless browser) - fastest, single binary
- Crawl4AI (Playwright-based) - most features
- FireCrawl (cloud API) - best for anti-bot protected sites
- Plain httpx - fastest for simple pages

The fetch layer automatically selects the best available backend based on
site characteristics and available tools.
"""

from core.fetch.obscura_client import (
    ObscuraConfig,
    ObscuraManager,
    get_playwright_browser_via_obscura,
    obscura_fetch,
    obscura_scrape,
    check_obscura_health,
)

__all__ = [
    "ObscuraConfig",
    "ObscuraManager",
    "get_playwright_browser_via_obscura",
    "obscura_fetch",
    "obscura_scrape",
    "check_obscura_health",
]