"""
Firecrawl-based web crawler for support content ingestion.

Workflow per brand:
  1. Fetch sitemap to discover policy/FAQ/help URLs
  2. Filter URLs by relevance patterns
  3. Crawl each page via Firecrawl (returns clean markdown)
  4. Yield CrawledPage objects to downstream extractor
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse

import structlog

from config import settings

log = structlog.get_logger(__name__)

# URL patterns that indicate support-relevant content
_RELEVANT_PATTERNS = re.compile(
    r"/(help|support|faq|returns?|refund|delivery|shipping|track|order|"
    r"size[-_]?guide|payment|contact|policy|terms|account|warranty)/",
    re.IGNORECASE,
)

# Hard-exclude: checkout, cart, login, admin, media assets
_EXCLUDE_PATTERNS = re.compile(
    r"/(checkout|cart|login|register|admin|cdn|static|media|\.jpg|\.png|\.css|\.js)",
    re.IGNORECASE,
)


@dataclass
class CrawledPage:
    url: str
    brand_id: str
    markdown: str
    title: str = ""
    crawled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(self.markdown.split())


# ── Firecrawl client (lazy) ───────────────────────────────────────────────────

_fc_client = None


def _get_fc():
    global _fc_client
    if _fc_client is None:
        from firecrawl import FirecrawlApp
        _fc_client = FirecrawlApp(api_key=settings.firecrawl_api_key)
    return _fc_client


# ═══════════════════════════════════════════════════════════════════════════


async def _fetch_sitemap_urls(base_url: str) -> list[str]:
    """Fetch and parse a sitemap.xml returning all page URLs."""
    import httpx

    sitemap_url = urljoin(base_url, "/sitemap.xml")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(sitemap_url, follow_redirects=True)
            if resp.status_code != 200:
                log.warning("sitemap_not_found", url=sitemap_url, status=resp.status_code)
                return []
            urls = re.findall(r"<loc>(https?://[^<]+)</loc>", resp.text)
            log.info("sitemap_fetched", url=sitemap_url, count=len(urls))
            return urls
    except Exception as exc:
        log.error("sitemap_fetch_failed", url=sitemap_url, error=str(exc))
        return []


def _is_relevant(url: str, domain: str) -> bool:
    """Return True if the URL is from the same domain and matches support patterns."""
    parsed = urlparse(url)
    if parsed.netloc and domain not in parsed.netloc:
        return False
    if _EXCLUDE_PATTERNS.search(url):
        return False
    if _RELEVANT_PATTERNS.search(url):
        return True
    # Also include any /help/* or direct help-centre-style paths
    path = parsed.path
    return bool(re.search(r"/(help|faq|policy|delivery|returns?)", path, re.I))


async def _scrape_page(url: str, brand_id: str) -> CrawledPage | None:
    """Scrape a single page via Firecrawl."""
    fc = _get_fc()
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: fc.scrape_url(url, params={"formats": ["markdown"], "onlyMainContent": True}),
        )
        if not result or not result.get("markdown"):
            log.debug("scrape_empty", url=url)
            return None

        page = CrawledPage(
            url=url,
            brand_id=brand_id,
            markdown=result["markdown"],
            title=result.get("metadata", {}).get("title", ""),
            metadata=result.get("metadata", {}),
        )
        log.debug("scrape_ok", url=url, words=page.word_count)
        return page
    except Exception as exc:
        log.error("scrape_failed", url=url, brand_id=brand_id, error=str(exc))
        return None


async def crawl_brand(
    brand_id: str,
    base_url: str,
    max_pages: int = 50,
    concurrency: int = 5,
) -> AsyncIterator[CrawledPage]:
    """
    Discover and crawl all support pages for a brand.

    Yields CrawledPage objects as they are scraped.
    """
    domain = urlparse(base_url).netloc
    log.info("crawl_start", brand_id=brand_id, base_url=base_url)

    # 1. Discover URLs via sitemap
    all_urls = await _fetch_sitemap_urls(base_url)

    # 2. Filter to relevant pages
    relevant = [u for u in all_urls if _is_relevant(u, domain)][:max_pages]
    log.info("crawl_urls_selected", brand_id=brand_id, total=len(all_urls), selected=len(relevant))

    if not relevant:
        log.warning("no_relevant_urls", brand_id=brand_id, base_url=base_url)
        return

    # 3. Scrape with bounded concurrency
    sem = asyncio.Semaphore(concurrency)

    async def bounded_scrape(url: str) -> CrawledPage | None:
        async with sem:
            return await _scrape_page(url, brand_id)

    tasks = [bounded_scrape(u) for u in relevant]
    for coro in asyncio.as_completed(tasks):
        page = await coro
        if page and page.word_count >= 50:  # skip near-empty pages
            yield page

    log.info("crawl_done", brand_id=brand_id, scraped=len(relevant))


async def crawl_url(brand_id: str, url: str) -> CrawledPage | None:
    """Scrape a single specific URL (for manual/on-demand ingestion)."""
    return await _scrape_page(url, brand_id)
