"""
Agent-driven brand data collection pipeline.

Architecture:
  1. Orchestrator agent  — finds the brand's help-center entry points via
     sitemap, DuckDuckGo search, and LLM URL generation.
  2. BFS collector (sub-agents in parallel) — starts at those entry points
     and dynamically follows links level by level, like a real user browsing.
     Each page is fetched with full stealth Playwright (headless Chrome,
     stealth JS patches, accordion expansion, simulated user behaviour).
     For Akamai/Cloudflare-protected pages the fetcher automatically falls
     back to Google Cache → Wayback Machine → httpx.
  3. Synthesis node — writes extracted items to FalkorDB + pgvector,
     applies any ontology proposals raised by sub-agents.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import textwrap
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from langgraph.graph import END, START, StateGraph
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from typing_extensions import TypedDict

from config import settings
from core.orchestration.llm import get_llm_client

log = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
_BFS_BATCH = 4               # concurrent pages per BFS round
_MAX_URLS_DEFAULT = 60       # total page cap per collection run
_FETCH_TIMEOUT = 25          # seconds per page fetch
_PAGE_FETCH_SOFT_LIMIT = 35  # seconds: warn if a single page takes longer
_MIN_CONTENT_LEN = 300       # skip pages shorter than this (404s, bot gates)
_COLLECTION_TIMEOUT = 1800   # 30 min hard cap on entire collection run
_HELP_KEYWORDS = [
    "help", "faq", "question", "support", "return", "refund", "cancel",
    "order", "ship", "deliver", "dispatch", "policy", "contact", "about",
    "customer-service", "helpcentre", "care", "exchange", "size", "payment",
]
_SKIP_PATH_FRAGMENTS = [
    # Commerce / account pages
    "/cart", "/checkout", "/login", "/register", "/account", "/wishlist",
    "/search", "/basket", "/bag",
    # Static assets
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    # Product / category browsing
    "/products/", "/product/", "/collections/", "/categories/",
    "/category/", "/womens/", "/mens/", "/girls/", "/boys/", "/kids/",
    "/beauty/", "/skincare/", "/new-in/", "/sale/", "/offers/",
    # Filtering / pagination params
    "?page=", "?sort=", "?colour=", "?color=", "?filter=", "?variant=",
    # Editorial / lifestyle / blog content (NOT help content)
    "/blog/", "/news/", "/editorial", "/lifestyle", "/inspiration",
    "/lookbook", "/look-book", "/magazine", "/journal", "/stories",
    "/the-debrief", "/debrief", "/community", "/events", "/press",
    "/careers", "/jobs", "/our-story", "/about-us",
    # Shopify /pages/ editorial sub-paths (help pages use /pages/informational/ etc.)
    "/pages/the-", "/pages/lifestyle", "/pages/editorial",
    "/pages/blog", "/pages/inspiration", "/pages/style",
    # Other non-help
    "/store-locator", "/sitemap",
]

_CATEGORIES = [
    "faq", "returns_policy", "shipping_policy", "contact",
    "about", "blog_news", "product_guide", "review",
]

# ── JS injected before each page to neutralise bot-detection signals ──────
_STEALTH_INIT_JS = """
(function() {
    try {
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [
            {name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},
            {name:'Native Client'}
        ]});
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US','en','en-GB']
        });
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : origQuery(params);
    } catch(e) {}
})();
"""

# JS to expand accordion/collapsible FAQ sections so the answers become
# part of the rendered DOM before crawl4ai takes the snapshot.
_EXPAND_CONTENT_JS = """
(async function() {
    const selectors = [
        'details:not([open]) > summary',
        '[aria-expanded="false"]',
        '.accordion-button.collapsed',
        '[data-bs-toggle="collapse"][aria-expanded="false"]',
        '.faq-item:not(.active) .faq-question',
        '.faq-question:not(.open)',
        '[role="button"][aria-expanded="false"]',
        '.collapsible-header',
        '.help-topic-item',
        '.question-row',
        '[data-expandable]',
    ];
    let clicked = 0;
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            try { el.click(); clicked++; } catch(e) {}
        }
    }
    if (clicked > 0) await new Promise(r => setTimeout(r, 1800));
    // Second pass for nested accordions revealed by first click
    for (const sel of selectors.slice(0,3)) {
        for (const el of document.querySelectorAll(sel)) {
            try { el.click(); } catch(e) {}
        }
    }
    if (clicked > 0) await new Promise(r => setTimeout(r, 800));
    return clicked;
})();
"""


# ── LangGraph state ────────────────────────────────────────────────────────

class AgentCollectState(TypedDict):
    brand_id: str
    brand_name: str
    base_url: str
    max_urls: int

    # Orchestrator output — entry-point URLs for the BFS collector
    entry_points: list[str]

    # BFS collector output
    extracted_items: list[dict[str, Any]]
    ontology_proposals: list[dict[str, Any]]

    phase: str
    errors: list[str]
    extracted_count: int
    started_at: str


# ═══════════════════════════════════════════════════════════════════════════
# FETCH LAYER  (stealth → cache → plain httpx)
# ═══════════════════════════════════════════════════════════════════════════

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)),
    reraise=False,
)
async def _httpx_get(url: str) -> str:
    """Plain fast fetch with exponential backoff — works for server-rendered pages."""
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
        ) as client:
            r = await client.get(url)
            if r.status_code < 400:
                return r.text
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
        raise  # let tenacity retry these
    except Exception as e:
        log.debug("httpx_get_failed", url=url, error=str(e))
    return ""


async def _firecrawl_fetch(url: str) -> str:
    """
    FireCrawl cloud API — handles JS rendering and anti-bot protection.
    Returns clean markdown. Only used when FIRECRAWL_API_KEY is configured.
    FireCrawl free tier: 500 credits/month (use sparingly, only when stealth fails).
    """
    if not settings.firecrawl_api_key:
        return ""
    try:
        from firecrawl import FirecrawlApp
        app = FirecrawlApp(api_key=settings.firecrawl_api_key)
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: app.scrape_url(url, formats=["markdown"])
        )
        # firecrawl-py v4 returns a ScrapeResponse pydantic model
        md = getattr(result, "markdown", None) or (result.get("markdown") if isinstance(result, dict) else None)
        if md and len(md.strip()) > _MIN_CONTENT_LEN:
            log.debug("firecrawl_ok", url=url, chars=len(md))
            return md
    except Exception as e:
        log.debug("firecrawl_failed", url=url, error=str(e))
    return ""


async def _stealth_crawl(url: str, expand: bool = True) -> str:
    """
    Full stealth Playwright fetch via crawl4ai:
    - enable_stealth (playwright-stealth plugin)
    - magic + simulate_user (randomised mouse / delays)
    - override_navigator (webdriver = undefined etc.)
    - JS accordion expansion
    - Non-headless Chrome UA
    """
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

        browser_cfg = BrowserConfig(
            headless=True,
            browser_type="chromium",
            user_agent_mode="random",
            enable_stealth=True,
            extra_args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--window-size=1920,1080",
            ],
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,en-GB;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
        )

        js_scripts = [_STEALTH_INIT_JS]
        if expand:
            js_scripts.append(_EXPAND_CONTENT_JS)

        run_cfg = CrawlerRunConfig(
            magic=True,
            simulate_user=True,
            override_navigator=True,
            js_code=js_scripts,
            delay_before_return_html=2.5 if expand else 1.5,
            word_count_threshold=5,
            page_timeout=30000,
            remove_overlay_elements=True,
        )

        t0 = time.monotonic()
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(url=url, config=run_cfg)
        elapsed = time.monotonic() - t0
        if elapsed > _PAGE_FETCH_SOFT_LIMIT:
            log.warning("slow_page_fetch", url=url[:80], elapsed_s=round(elapsed, 1))
        # Prefer raw HTML: lxml link extraction needs <a href> tags.
        # trafilatura will clean it for the LLM extraction step.
        return result.html or result.markdown or ""

    except TypeError as e:
        log.debug("stealth_type_error", url=url, reason=str(e))
    except asyncio.TimeoutError:
        log.warning("stealth_timeout", url=url[:80])
    except Exception as e:
        log.debug("stealth_failed", url=url, error=str(e))
    return ""


async def _google_cache(url: str) -> str:
    """Google Cache fallback — useful for pages behind Akamai/Cloudflare."""
    cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
    text = await _httpx_get(cache_url)
    if text and len(text.strip()) > 500 and "cache" in text.lower():
        return text
    return ""


async def _wayback_machine(url: str) -> str:
    """Wayback Machine (Archive.org) fallback."""
    try:
        avail = await _httpx_get(
            f"https://archive.org/wayback/available?url={url}"
        )
        if avail:
            snap = json.loads(avail).get("archived_snapshots", {}).get("closest", {})
            arch_url = snap.get("url", "")
            if arch_url and snap.get("available"):
                text = await _httpx_get(arch_url)
                if text and len(text) > 500:
                    log.info("wayback_hit", url=url, archive=arch_url)
                    return text
    except Exception as e:
        log.debug("wayback_failed", url=url, error=str(e))
    return ""


async def _fetch_any(url: str, expand: bool = True) -> str:
    """
    Fetch strategy waterfall (most capable → fastest):
      1. Stealth crawl4ai  (handles JS SPAs, expands accordions)
      2. FireCrawl          (cloud anti-bot bypass — only if API key set)
      3. Google Cache       (bypasses Akamai / rate-limiting)
      4. Wayback Machine    (archival fallback)
      5. Plain httpx        (fast, for simple static pages)

    Returns the richest non-empty response found.
    """
    # Strategy 1: stealth browser
    content = await _stealth_crawl(url, expand=expand)
    if len(content.strip()) >= _MIN_CONTENT_LEN:
        log.debug("fetch_stealth_ok", url=url, chars=len(content))
        return content

    # Strategy 2: FireCrawl (cloud — handles Cloudflare/Akamai when stealth fails)
    fc = await _firecrawl_fetch(url)
    if len(fc.strip()) >= _MIN_CONTENT_LEN:
        log.debug("fetch_firecrawl_ok", url=url, chars=len(fc))
        return fc

    # Strategy 3: Google Cache
    cached = await _google_cache(url)
    if len(cached.strip()) >= _MIN_CONTENT_LEN:
        log.debug("fetch_cache_ok", url=url, chars=len(cached))
        return cached

    # Strategy 4: Wayback Machine
    archived = await _wayback_machine(url)
    if len(archived.strip()) >= _MIN_CONTENT_LEN:
        log.debug("fetch_wayback_ok", url=url, chars=len(archived))
        return archived

    # Strategy 5: plain httpx
    plain = await _httpx_get(url)
    if plain:
        log.debug("fetch_httpx_ok", url=url, chars=len(plain))
        return plain

    log.info("fetch_all_failed", url=url)
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# LINK HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _extract_links(html: str, base_url: str, same_domain_only: bool = True) -> list[str]:
    """
    Extract clean on-domain links from HTML using lxml DOM parser.
    Falls back to regex for markdown/plain-text content.
    """
    base_p = urlparse(base_url)
    links: list[str] = []

    def _is_valid_path(path: str) -> bool:
        """Reject paths with embedded hostnames or email addresses in segments.
        Catches: /pages/informational/www.ravelin.com/...  and  /DPO@boohoo.com"""
        if "@" in path:
            return False
        for seg in path.split("/"):
            # Path segment that starts with www. is always a mis-resolved external URL
            if seg.startswith("www."):
                return False
        return True

    # Try lxml DOM parsing (real links only — no regex hallucination)
    if "<a " in html or "<A " in html:
        try:
            import lxml.html
            root = lxml.html.fromstring(html, base_url=base_url)
            root.make_links_absolute(base_url, resolve_base_href=True)
            for el, _, href, _ in root.iterlinks():
                if el.tag.lower() != "a" or not href:
                    continue
                p = urlparse(href)
                if p.scheme not in ("http", "https"):
                    continue
                if same_domain_only and p.netloc != base_p.netloc:
                    continue
                if not _is_valid_path(p.path):
                    continue
                clean = f"{p.scheme}://{p.netloc}{p.path}"
                if any(skip in clean.lower() for skip in _SKIP_PATH_FRAGMENTS):
                    continue
                links.append(clean)
            return list(dict.fromkeys(links))
        except Exception:
            pass

    # Fallback: regex for markdown-style [text](url) or href= patterns
    for m in re.finditer(r'href=["\']([^"\'#\s]+)["\']|\]\(([^)\s#]+)\)', html):
        href = m.group(1) or m.group(2)
        if not href:
            continue
        full = urljoin(base_url, href)
        p = urlparse(full)
        if same_domain_only and p.netloc != base_p.netloc:
            continue
        if p.scheme not in ("http", "https"):
            continue
        if not _is_valid_path(p.path):
            continue
        clean = f"{p.scheme}://{p.netloc}{p.path}"
        if any(skip in clean.lower() for skip in _SKIP_PATH_FRAGMENTS):
            continue
        links.append(clean)
    return list(dict.fromkeys(links))


def _is_help_link(url: str) -> bool:
    return any(kw in url.lower() for kw in _HELP_KEYWORDS)


def _guess_category(url: str) -> str:
    u = url.lower()
    if any(k in u for k in ["return", "refund", "exchange"]):
        return "returns_policy"
    if any(k in u for k in ["ship", "deliver", "dispatch"]):
        return "shipping_policy"
    if any(k in u for k in ["contact", "reach-us"]):
        return "contact"
    if any(k in u for k in ["about", "our-story"]):
        return "about"
    return "faq"


# ═══════════════════════════════════════════════════════════════════════════
# DISCOVERY HELPERS (for the orchestrator)
# ═══════════════════════════════════════════════════════════════════════════

async def _sitemap_help_urls(base_url: str):
    domain = urlparse(base_url).netloc
    for path in ["/sitemap.xml", "/sitemap_index.xml"]:
        text = await _httpx_get(f"https://{domain}{path}")
        if not text or "<" not in text:
            continue
        try:
            root = ET.fromstring(text)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            # sub-sitemaps
            for sub in [e.text for e in root.findall(".//sm:loc", ns) if e.text and "sitemap" in e.text.lower()][:3]:
                st = await _httpx_get(sub)
                if st:
                    try:
                        sr = ET.fromstring(st)
                        for e in sr.findall(".//sm:loc", ns):
                            if e.text and _is_help_link(e.text):
                                yield e.text
                    except ET.ParseError:
                        pass
            # direct locs
            for e in root.findall(".//sm:loc", ns):
                if e.text and "sitemap" not in e.text and _is_help_link(e.text):
                    yield e.text
            return
        except ET.ParseError:
            continue


async def _ddg_search(brand_name: str, domain: str) -> list[str]:
    """
    DuckDuckGo search for brand help pages.
    Primary: duckduckgo-search library (structured results, more reliable).
    Fallback: DuckDuckGo HTML scraping.
    """
    domain_base = domain.replace("www.", "")
    query = f'site:{domain_base} help OR faq OR "returns policy" OR "customer service"'
    found: list[str] = []

    # Primary: ddgs library (formerly duckduckgo-search) with 15s timeout
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        results = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: list(DDGS().text(query, max_results=25))
            ),
            timeout=15.0,
        )
        for r in results:
            href = r.get("href", "")
            if href and domain_base in href and href.startswith("http"):
                found.append(href.split("?")[0])
        if found:
            log.debug("ddg_library_ok", domain=domain_base, count=len(found))
            return list(dict.fromkeys(found))[:30]
    except asyncio.TimeoutError:
        log.info("ddg_library_timeout", domain=domain_base)
    except Exception as e:
        # Non-critical: AI research agent compensates for missing DDG results
        log.info("ddg_library_unavailable", domain=domain_base, reason=str(e)[:80])

    # Fallback: DuckDuckGo HTML scraping (also with timeout)
    try:
        import urllib.parse as _urlparse
        encoded = _urlparse.quote(query, safe="+")
        text = await asyncio.wait_for(
            _httpx_get(f"https://html.duckduckgo.com/html/?q={encoded}"),
            timeout=10.0,
        )
        if text:
            for m in re.finditer(r'uddg=([^&"\']+)', text):
                href = _urlparse.unquote(m.group(1))
                if domain_base in href and href.startswith("http"):
                    found.append(href.split("?")[0])
            for m in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"', text):
                href = m.group(1)
                if domain_base in href and href.startswith("http"):
                    found.append(href.split("?")[0])
    except (asyncio.TimeoutError, Exception) as e:
        log.info("ddg_html_unavailable", domain=domain_base, reason=str(e)[:60])

    return list(dict.fromkeys(found))[:30]


async def _llm_entry_points(llm, brand_name: str, base_url: str, known: list[str]) -> list[str]:
    """LLM generates likely help-center entry point URLs."""
    domain = urlparse(base_url).netloc
    known_txt = "\n".join(f"- {u}" for u in known[:15]) or "(none found)"
    prompt = textwrap.dedent(f"""
        Brand: {brand_name}   Domain: {base_url}
        Known pages so far:\n{known_txt}

        Generate up to 10 realistic entry-point URLs for this brand's customer
        support help centre (FAQ, returns, shipping, contact, help centre root).
        Only URLs on {domain}. Respond with a JSON array only:
        ["https://...", ...]
    """).strip()
    try:
        raw = await llm.generate(prompt, max_tokens=512, fast=True)
        m = re.search(r'\[[\s\S]+?\]', raw)
        if m:
            urls = json.loads(m.group(0))
            return [u for u in urls if isinstance(u, str) and domain in u and u.startswith("http")]
    except Exception as e:
        log.debug("llm_entry_failed", error=str(e))
    return []


async def _ai_research_help_center(
    llm,
    brand_name: str,
    base_url: str,
    homepage_html: str,
    ddg_results: list[str],
) -> list[str]:
    """
    AI research agent: given the homepage HTML and DDG results, reason about
    where this brand's customer help/support centre actually lives.

    The agent looks at every real link on the homepage plus search results,
    then returns the URLs most likely to contain FAQs, policies, and support
    content — filtering out product pages, category listings, and other
    non-support pages.
    """
    domain = urlparse(base_url).netloc

    # Extract all real DOM links from the homepage
    all_links = _extract_links(homepage_html, base_url, same_domain_only=False)
    # Focus: same-domain + subdomains (e.g. support.brand.com)
    base_domain = domain.lstrip("www.")
    relevant_links = [u for u in all_links if base_domain in u][:80]

    links_section = "\n".join(relevant_links) if relevant_links else "(none found)"
    ddg_section = "\n".join(ddg_results[:20]) if ddg_results else "(none found)"

    prompt = textwrap.dedent(f"""
        You are a research agent finding the customer support section of an e-commerce website.

        Brand: {brand_name}
        Website: {base_url}

        === Links found on the homepage ===
        {links_section}

        === DuckDuckGo search results for "{brand_name} help centre FAQ returns" ===
        {ddg_section}

        Task: Identify ALL URLs that lead to customer support content:
        - Help centre / FAQ pages
        - Returns & refund policy
        - Shipping & delivery policy
        - Contact / live chat
        - Order tracking
        - Size guides
        - Any subdomain like support.{base_domain} or help.{base_domain}

        EXCLUDE: product pages, category listings, checkout, login, blog posts, sale pages.

        Return ONLY a JSON array of URLs, most important first. Include the full URL.
        If the help centre is on a subdomain (support.{base_domain}), include it.
        ["https://...", ...]
    """).strip()

    try:
        raw = await llm.generate(prompt, max_tokens=1024, fast=False)
        m = re.search(r'\[[\s\S]+?\]', raw)
        if m:
            urls = json.loads(m.group(0))
            valid = [
                u for u in urls
                if isinstance(u, str) and u.startswith("http") and base_domain in u
                and not any(skip in u.lower() for skip in _SKIP_PATH_FRAGMENTS)
            ]
            log.info("ai_research_found", brand=brand_name, count=len(valid), urls=valid[:5])
            return valid
    except Exception as e:
        log.warning("ai_research_failed", error=str(e))
    return []


# ═══════════════════════════════════════════════════════════════════════════
# CONTENT EXTRACTION HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _extract_clean_text(html_or_markdown: str) -> str:
    """
    Convert raw HTML to clean readable text via trafilatura.
    trafilatura removes navigation bars, headers, footers, sidebars,
    and script/style tags — leaving only the main body content.
    Falls back to regex stripping if trafilatura returns nothing.
    """
    # If it's already markdown (crawl4ai returns .markdown), it's mostly clean
    if not ("<html" in html_or_markdown.lower() or "<body" in html_or_markdown.lower()):
        clean = re.sub(r'\s+', ' ', html_or_markdown).strip()
        return clean

    try:
        import trafilatura
        text = trafilatura.extract(
            html_or_markdown,
            include_tables=True,
            include_links=False,
            favor_precision=False,
            no_fallback=False,
        )
        if text and len(text.strip()) > 100:
            return text.strip()
    except Exception:
        pass

    # Regex fallback
    clean = re.sub(r'<[^>]+>', ' ', html_or_markdown)
    return re.sub(r'\s+', ' ', clean).strip()


async def _validate_candidates(urls: list[str], timeout: float = 8.0) -> list[str]:
    """
    HEAD-check each URL, keeping only those that respond with <400.
    Boohoo's catch-all redirects 404s to a 200 offers page — we can't
    distinguish those via HEAD alone, but we CAN reject hard 404s and
    connection errors so LLM-hallucinated paths don't pollute the queue.
    """
    async def _alive(url: str) -> str | None:
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            ) as client:
                r = await client.head(url)
                return url if r.status_code < 400 else None
        except Exception:
            return None

    tasks = [_alive(u) for u in urls]
    results = await asyncio.gather(*tasks)
    valid = [u for u in results if u is not None]
    log.info("url_validation", total=len(urls), valid=len(valid))
    return valid


# ═══════════════════════════════════════════════════════════════════════════
# LLM EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def _is_help_content(text: str) -> bool:
    """Quick check: does this text contain any customer support content?"""
    signals = [
        "return", "refund", "shipping", "delivery", "order", "cancel",
        "how do i", "how can i", "faq", "frequently asked", "contact us",
        "customer service", "exchange", "size guide", "payment",
        "track", "dispatch", "policy", "help", "support",
    ]
    text_lower = text.lower()
    return sum(1 for s in signals if s in text_lower) >= 3


def _find_content_chunks(text: str, chunk_size: int = 6000, max_chunks: int = 6) -> list[str]:
    """
    Split page text into overlapping chunks, prioritising sections that
    contain customer-support signals.

    Strategy:
    1. Find all positions where help-related keywords occur
    2. Build chunks centred on those positions (deduplicated by overlap)
    3. Fall back to sequential chunks if no keywords found

    This solves the problem of a 230K-char page where FAQs live at position
    40K — we find them instead of blindly windowing the first/middle 8K.
    """
    if not text:
        return []

    # Keyword positions that signal help/support content
    markers = [
        "how do i", "how can i", "what is", "what are", "can i",
        "do you", "when will", "how long", "free return", "return policy",
        "refund", "shipping policy", "delivery time", "frequently asked",
        "faq", "help &", "contact us", "size guide", "payment method",
        "how to return", "how to track", "cancel my order",
    ]
    text_lower = text.lower()
    anchor_positions: list[int] = []
    for marker in markers:
        idx = 0
        while True:
            idx = text_lower.find(marker, idx)
            if idx == -1:
                break
            anchor_positions.append(max(0, idx - 200))
            idx += len(marker)

    if anchor_positions:
        # Sort and deduplicate anchors that are < chunk_size apart
        anchor_positions.sort()
        deduplicated: list[int] = [anchor_positions[0]]
        for pos in anchor_positions[1:]:
            if pos - deduplicated[-1] > chunk_size * 0.7:
                deduplicated.append(pos)

        chunks: list[str] = []
        for start in deduplicated[:max_chunks]:
            chunk = text[start: start + chunk_size]
            if len(chunk.strip()) >= _MIN_CONTENT_LEN:
                chunks.append(chunk)
        if chunks:
            return chunks

    # Fallback: sequential chunks (skip first 10% which is usually nav)
    skip = min(len(text) // 10, 3000)
    chunks = []
    pos = skip
    while pos < len(text) and len(chunks) < max_chunks:
        chunk = text[pos: pos + chunk_size]
        if len(chunk.strip()) >= _MIN_CONTENT_LEN:
            chunks.append(chunk)
        pos += chunk_size
    return chunks


async def _extract_chunk(
    llm,
    url: str,
    category: str,
    chunk: str,
    brand_id: str,
    brand_name: str,
    ontology_labels: str,
) -> tuple[list[dict], list[dict]]:
    """Run LLM extraction on a single content chunk."""
    prompt = textwrap.dedent(f"""
        Brand: {brand_name} (brand_id: {brand_id})
        URL: {url}  |  Category hint: {category}
        Already-known entity types: {ontology_labels}

        Page content snippet:
        {chunk}

        Task 1 — EXTRACT every piece of customer-support knowledge:
        Every Q&A pair, policy clause, shipping rule, size guide entry, contact
        detail, order/returns step, or any fact a customer-service agent would use.
        Map each item to the best known entity type (FAQ, Policy, KnowledgeItem,
        Review, ProductCategory, or any custom type you propose in Task 2).

        Task 2 — PROPOSE new entity types for content that doesn't fit the known types.
        Examples of good proposals: SizeGuide, WarrantyPolicy, LoyaltyProgram,
        DeliveryZone, ContactMethod, OrderStatus, PromotionPolicy, etc.
        Only propose if you have DIRECT EVIDENCE in this specific snippet.
        The "evidence" field must quote or specifically describe what you saw —
        e.g. "Page shows a size table with UK 8-22 → EU 36-50 conversions and hip/waist/bust measurements"
        NOT vague phrases like "this page contains size information".

        Return ONLY valid JSON (no markdown fences):
        {{
            "items": [
                {{
                    "entity_type": "FAQ",
                    "title": "Short question or heading",
                    "content": "Full answer or policy text",
                    "category": "returns_policy",
                    "keywords": ["returns", "refund", "30 days"]
                }},
                {{
                    "entity_type": "SizeGuide",
                    "title": "Women's Dress Size Guide",
                    "content": "UK 8 = EU 36 = US 4 ...",
                    "category": "sizing",
                    "keywords": ["size", "measurements"]
                }}
            ],
            "ontology_proposals": [
                {{
                    "label": "SizeGuide",
                    "description": "Brand size guide tables for clothing categories with UK/EU/US measurements",
                    "properties": ["brand_id", "category", "size_chart", "measurement_unit", "size_range"],
                    "relationships": [["Brand", "HAS_SIZE_GUIDE", "incoming"]],
                    "evidence": "Page shows a table mapping UK 6-22 → EU 34-50 → US 2-18 with bust/waist/hip measurements in cm and inches across 5 clothing categories"
                }}
            ]
        }}

        Return {{"items":[],"ontology_proposals":[]}} ONLY if this snippet
        contains ZERO customer-support content (pure navigation, product listing,
        bot-gate, or 404 redirect content).
    """).strip()

    try:
        raw = await llm.generate(prompt, max_tokens=4096, fast=False)
        m = re.search(r'\{[\s\S]+\}', raw)
        if not m:
            log.debug("chunk_no_json", url=url, raw_preview=raw[:120])
            return [], []
        data = json.loads(m.group(0))
        return data.get("items", []), data.get("ontology_proposals", [])
    except json.JSONDecodeError as e:
        log.debug("chunk_json_error", url=url, error=str(e))
        return [], []
    except Exception as e:
        log.warning("chunk_extract_failed", url=url, error=str(e))
        return [], []


async def _llm_extract(
    llm,
    url: str,
    category: str,
    content: str,
    brand_id: str,
    brand_name: str,
    ontology_labels: str,
) -> tuple[list[dict], list[dict]]:
    """
    Extract structured knowledge from a page.

    Large pages (e.g. 230K char Shopify FAQ) are split into keyword-anchored
    chunks so the LLM always sees content near FAQ/policy sections rather than
    a random 8K window from the middle.
    """
    # Use trafilatura to get clean body text (strips nav/boilerplate)
    clean = _extract_clean_text(content)

    if len(clean) < _MIN_CONTENT_LEN:
        log.debug("extract_too_short", url=url, chars=len(clean))
        return [], []

    # Pre-check: skip pages with no help signals.
    # Check BOTH the cleaned text AND a slice of the raw content — trafilatura
    # can strip JS-rendered accordion answers, making the cleaned text look empty
    # even when the underlying HTML contains full FAQ content.
    raw_sample = re.sub(r'<[^>]+>', ' ', content[:40000])
    raw_sample = re.sub(r'\s+', ' ', raw_sample)
    if not _is_help_content(clean) and not _is_help_content(raw_sample):
        log.info("extract_skipped_no_help_signals", url=url, chars=len(clean))
        return [], []

    # Split into targeted chunks
    chunks = _find_content_chunks(clean, chunk_size=6000, max_chunks=5)
    if not chunks:
        log.info("extract_no_chunks", url=url)
        return [], []

    log.info("extract_chunks", url=url[:70], chunks=len(chunks), total_chars=len(clean))

    # Process all chunks in parallel
    tasks = [
        _extract_chunk(llm, url, category, chunk, brand_id, brand_name, ontology_labels)
        for chunk in chunks
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge and deduplicate by title
    all_items: list[dict] = []
    all_proposals: list[dict] = []
    seen_titles: set[str] = set()

    for res in results:
        if isinstance(res, Exception):
            log.warning("chunk_task_failed", url=url, error=str(res))
            continue
        items, proposals = res
        for item in items:
            title = item.get("title", "").strip().lower()
            if title and title not in seen_titles:
                seen_titles.add(title)
                item["brand_id"] = brand_id
                item["source_url"] = url
                item["extracted_at"] = datetime.now(timezone.utc).isoformat()
                all_items.append(item)
        all_proposals.extend(proposals)

    log.info("extract_done", url=url[:70], items=len(all_items), proposals=len(all_proposals))
    return all_items, all_proposals


# ═══════════════════════════════════════════════════════════════════════════
# NODE 1 — Orchestrator: find help-center entry points
# ═══════════════════════════════════════════════════════════════════════════

async def node_orchestrate(state: AgentCollectState) -> dict[str, Any]:
    """
    AI-driven discovery of the brand's help-centre entry points.

    Strategy order:
    1. Fetch homepage HTML (browser-rendered, stealth)
    2. DuckDuckGo + sitemap run in parallel
    3. AI research agent: analyses homepage links + DDG results to identify
       exactly which URLs are help/support/policy pages (not product pages)
    4. LLM URL generation as last-resort fallback
    """
    base_url = state["base_url"]
    domain = urlparse(base_url).netloc
    llm = get_llm_client()
    log.info("orchestrator_start", brand_id=state["brand_id"], base_url=base_url)

    candidates: list[str] = []
    seen: set[str] = set()

    def _add(urls):
        for u in urls:
            clean = u.split("?")[0].rstrip("/")
            if clean not in seen:
                seen.add(clean)
                candidates.append(u)

    # ── Step 1: fetch homepage + sitemap + DDG in parallel ─────────────────
    main_html_task = asyncio.create_task(_stealth_crawl(base_url, expand=False))

    sitemap_task = asyncio.create_task(_collect_sitemap(base_url))
    ddg_task = asyncio.create_task(_ddg_search(state["brand_name"], domain))

    main_html, sitemap_urls, ddg = await asyncio.gather(
        main_html_task, sitemap_task, ddg_task, return_exceptions=True
    )
    if isinstance(main_html, Exception): main_html = ""
    if isinstance(sitemap_urls, Exception): sitemap_urls = []
    if isinstance(ddg, Exception): ddg = []

    log.info("discovery_raw", sitemap=len(sitemap_urls), ddg=len(ddg), html_chars=len(main_html))

    # Add sitemap help URLs directly (already filtered by _is_help_link)
    _add(sitemap_urls)
    _add(ddg)

    # ── Step 2: AI research agent ──────────────────────────────────────────
    # Feed homepage HTML + DDG results to the LLM; it distinguishes help pages
    # from product/category pages using reasoning, not just keyword matching.
    if main_html:
        ai_urls = await _ai_research_help_center(
            llm, state["brand_name"], base_url, main_html, ddg
        )
        # AI-found URLs go to the FRONT — they're the most targeted
        ai_urls_deduped = [u for u in ai_urls if u.split("?")[0].rstrip("/") not in seen]
        candidates = ai_urls_deduped + candidates
        for u in ai_urls_deduped:
            seen.add(u.split("?")[0].rstrip("/"))
        log.info("ai_research_added", count=len(ai_urls_deduped))

    # ── Step 3: LLM URL generation if still sparse ─────────────────────────
    if len(candidates) < 5:
        llm_urls = await _llm_entry_points(llm, state["brand_name"], base_url, candidates)
        log.info("llm_entry_points", count=len(llm_urls))
        _add(llm_urls)

    # Ensure the base URL is always reachable as a last resort
    _add([base_url])

    log.info(
        "orchestrator_done",
        brand_id=state["brand_id"],
        entry_points=len(candidates),
        top5=candidates[:5],
    )
    return {
        "entry_points": candidates[: state["max_urls"]],
        "phase": "collect",
        "errors": [],
    }


async def _collect_sitemap(base_url: str) -> list[str]:
    """Collect help-related URLs from sitemap (wrapper for async generator)."""
    urls: list[str] = []
    async for u in _sitemap_help_urls(base_url):
        urls.append(u)
        if len(urls) >= 50:
            break
    return urls


# ═══════════════════════════════════════════════════════════════════════════
# NODE 2 — BFS collector: dynamically follows links, expands content
# ═══════════════════════════════════════════════════════════════════════════


async def _crawl4ai_bfs(
    entry_url: str,
    domain: str,
    max_pages: int,
) -> list[tuple[str, str]]:
    """
    Use crawl4ai BFSDeepCrawlStrategy to crawl from entry_url.
    Only follows links that actually exist in the DOM — no hallucinated paths.
    Returns list of (url, content) pairs.
    """
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

    browser_cfg = BrowserConfig(
        headless=True,
        browser_type="chromium",
        user_agent_mode="random",
        enable_stealth=True,
        extra_args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--window-size=1920,1080",
        ],
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,en-GB;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )

    from crawl4ai.deep_crawling.filters import FilterChain, DomainFilter, URLPatternFilter

    # Allow both bare domain and www variant (sites redirect between them)
    domain_variants = [domain]
    if domain.startswith("www."):
        domain_variants.append(domain[4:])
    else:
        domain_variants.append(f"www.{domain}")

    # Block product/cart/asset paths; only follow help/support content
    block_patterns = [f"*{frag}*" for frag in _SKIP_PATH_FRAGMENTS]

    strategy = BFSDeepCrawlStrategy(
        max_depth=3,
        max_pages=max_pages,
        include_external=False,
        filter_chain=FilterChain([
            DomainFilter(allowed_domains=domain_variants),
            URLPatternFilter(patterns=block_patterns, use_glob=True, reverse=True),
        ]),
    )

    run_cfg = CrawlerRunConfig(
        deep_crawl_strategy=strategy,
        magic=True,
        simulate_user=True,
        override_navigator=True,
        js_code=[_STEALTH_INIT_JS, _EXPAND_CONTENT_JS],
        delay_before_return_html=2.5,
        word_count_threshold=5,
        page_timeout=45000,
        remove_overlay_elements=True,
    )

    pages: list[tuple[str, str]] = []
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        results = await crawler.arun(url=entry_url, config=run_cfg)

    # CrawlResultContainer (0.8.x) is iterable — iterate directly
    for r in results:
        url = getattr(r, 'url', entry_url)
        content = getattr(r, 'html', None) or getattr(r, 'markdown', '') or ''
        if not content or len(content.strip()) < _MIN_CONTENT_LEN:
            continue
        # Accept both bare domain and www variant
        netloc = urlparse(url).netloc
        if netloc not in domain_variants:
            continue
        if any(skip in url.lower() for skip in _SKIP_PATH_FRAGMENTS):
            continue
        pages.append((url, content))

    log.info("bfs_deep_results", entry=entry_url, total_crawled=len(list(results)) if hasattr(results, '__len__') else '?', kept=len(pages))
    return pages


async def node_collect(state: AgentCollectState) -> dict[str, Any]:
    """
    Collect pages from entry points.

    Primary:  crawl4ai BFSDeepCrawlStrategy — browser renders each page,
              extracts links from the real DOM, and follows them level by level.
              This ensures we only visit URLs that actually exist on the site.

    Fallback: manual BFS with fetch waterfall (stealth → cache → wayback → httpx).
    """
    entry_points = state["entry_points"]
    if not entry_points:
        log.warning("collect_no_entry_points", brand_id=state["brand_id"])
        return {
            "extracted_items": [],
            "ontology_proposals": [],
            "phase": "synthesize",
            "errors": state["errors"],
        }

    domain = urlparse(state["base_url"]).netloc
    llm = get_llm_client()
    brand_id = state["brand_id"]
    brand_name = state["brand_name"]
    max_pages = state["max_urls"]

    ontology_labels = "FAQ, Policy, KnowledgeItem, Review, ProductCategory"
    try:
        from core.ontology.dynamic import get_ontology_manager
        ontology_labels = get_ontology_manager().get_type_labels_for_prompt()
    except Exception:
        pass

    all_items: list[dict] = []
    all_proposals: list[dict] = []
    errors = list(state["errors"])
    crawled_pages: list[tuple[str, str]] = []

    domain_variants = {domain}
    if domain.startswith("www."):
        domain_variants.add(domain[4:])
    else:
        domain_variants.add(f"www.{domain}")

    # ── Strategy 1: crawl4ai BFSDeepCrawlStrategy ─────────────────────────
    # Start from the help-keyword entry point identified by the AI research agent.
    help_entry = next((u for u in entry_points if _is_help_link(u)), None)
    start_url = help_entry or entry_points[0]
    log.info("bfs_deep_start", brand_id=brand_id, start_url=start_url, max_pages=max_pages)
    try:
        crawled_pages = await _crawl4ai_bfs(start_url, domain, max_pages)
        log.info("bfs_deep_done", brand_id=brand_id, pages=len(crawled_pages))
    except Exception as e:
        log.warning("bfs_deep_failed", brand_id=brand_id, error=str(e))
        crawled_pages = []

    # ── Strategy 2: manual BFS — always runs on remaining uncovered entries ──
    # Deep BFS covers the best entry point depth-3; manual BFS covers the rest.
    # Also acts as primary when deep BFS fails or returns < 5 pages.
    already_visited = {u.split("?")[0].rstrip("/") for u, _ in crawled_pages}
    remaining_entries = [
        u for u in entry_points
        if u.split("?")[0].rstrip("/") not in already_visited
        and _is_help_link(u)  # only visit help-keyword URLs in manual mode
    ]
    if remaining_entries or len(crawled_pages) < 5:
        queue_seed = remaining_entries if crawled_pages else list(entry_points)
        log.info("bfs_manual_start", brand_id=brand_id, queue=len(queue_seed))
        visited: set[str] = set(already_visited)
        queue: list[str] = queue_seed
        bfs_start = time.monotonic()
        round_num = 0

        while queue and len(visited) < max_pages:
            # Hard cap: don't let BFS run forever on slow sites
            if time.monotonic() - bfs_start > _COLLECTION_TIMEOUT:
                log.warning("bfs_manual_timeout", brand_id=brand_id,
                            elapsed_s=round(time.monotonic() - bfs_start, 1),
                            pages_so_far=len(crawled_pages))
                break

            batch: list[str] = []
            while queue and len(batch) < _BFS_BATCH:
                url = queue.pop(0)
                norm = url.split("?")[0].rstrip("/")
                if norm not in visited:
                    visited.add(norm)
                    batch.append(url)
            if not batch:
                break

            round_num += 1
            log.info("bfs_round", brand_id=brand_id, round=round_num,
                     batch=len(batch), pages_collected=len(crawled_pages),
                     visited=len(visited), queued=len(queue))
            fetch_tasks = [_fetch_any(u, expand=True) for u in batch]
            fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            for url, content in zip(batch, fetch_results):
                if isinstance(content, Exception) or not content:
                    continue
                if len(content.strip()) >= _MIN_CONTENT_LEN:
                    crawled_pages.append((url, content))
                    for link in _extract_links(content, f"https://{domain}"):
                        if _is_help_link(link):
                            norm = link.split("?")[0].rstrip("/")
                            if norm not in visited:
                                queue.append(link)

            await asyncio.sleep(0.5 + random.random() * 0.5)

        log.info("bfs_manual_done", brand_id=brand_id, pages=len(crawled_pages),
                 rounds=round_num, elapsed_s=round(time.monotonic() - bfs_start, 1))

    # ── Extract knowledge from all collected pages ─────────────────────────
    log.info("extract_start", brand_id=brand_id, pages=len(crawled_pages))
    extract_tasks = [
        _llm_extract(llm, url, _guess_category(url), content, brand_id, brand_name, ontology_labels)
        for url, content in crawled_pages
    ]
    extract_results = await asyncio.gather(*extract_tasks, return_exceptions=True)

    for result in extract_results:
        if isinstance(result, Exception):
            errors.append(str(result))
            continue
        items, proposals = result
        all_items.extend(items)
        all_proposals.extend(proposals)

    if len(all_items) == 0 and len(crawled_pages) > 0:
        log.warning(
            "collect_zero_items",
            brand_id=brand_id,
            pages_crawled=len(crawled_pages),
            hint="All pages failed help-signal check — BFS may have landed on product/editorial pages. "
                 "Check _SKIP_PATH_FRAGMENTS and AI research agent entry points.",
        )
    log.info(
        "collect_done",
        brand_id=brand_id,
        pages=len(crawled_pages),
        items=len(all_items),
        proposals=len(all_proposals),
        errors=len(errors),
    )
    return {
        "extracted_items": all_items,
        "ontology_proposals": all_proposals,
        "phase": "synthesize",
        "errors": errors,
    }


# ═══════════════════════════════════════════════════════════════════════════
# NODE 3 — Synthesis: write to FalkorDB + pgvector, apply ontology
# ═══════════════════════════════════════════════════════════════════════════

def _valid_node_label(entity_type: str) -> str:
    """
    Validate that entity_type is safe to use as a FalkorDB node label.
    Labels must start with a letter and contain only alphanumeric + underscore.
    Falls back to 'KnowledgeItem' for anything that doesn't qualify.
    """
    if entity_type and re.match(r'^[A-Za-z][A-Za-z0-9_]*$', entity_type):
        return entity_type
    return "KnowledgeItem"


async def _write_graph(items: list[dict], brand_id: str) -> int:
    """
    Write extracted items to FalkorDB using the correct ontology-aligned node label.

    Node label mapping:
    - entity_type=FAQ          → :FAQ node (question/answer schema)
    - entity_type=Policy       → :Policy node (policy_type/content schema)
    - entity_type=SizeGuide,
      ContactMethod, etc.      → :SizeGuide, :ContactMethod, etc.
                                 (ontology-registered custom types)
    - anything else            → :KnowledgeItem (generic fallback)
    """
    written = 0
    try:
        from core.ontology.schema import get_graph_client
        _, graph = get_graph_client()
        for item in items:
            et = item.get("entity_type", "KnowledgeItem")
            title = str(item.get("title", ""))[:200].replace("'", "")
            content = str(item.get("content", ""))[:2000].replace("'", "")
            cat = str(item.get("category", "general")).replace("'", "")
            src = str(item.get("source_url", "")).replace("'", "")
            kw = json.dumps(item.get("keywords", [])).replace("'", "")
            nid = f"{brand_id}_{uuid.uuid4().hex[:8]}"

            if et == "FAQ":
                graph.query(
                    f"MERGE (n:FAQ {{id:'{nid}'}}) "
                    f"SET n.question='{title}',n.answer='{content}',"
                    f"    n.category='{cat}',n.keywords='{kw}',"
                    f"    n.brand_id='{brand_id}',n.source_url='{src}' "
                    f"WITH n MATCH (b:Brand {{id:'{brand_id}'}}) MERGE (b)-[:HAS_FAQ]->(n)"
                )
            elif et in ("Policy", "returns_policy", "shipping_policy"):
                pt = cat if cat in ("returns", "shipping", "privacy") else "general"
                graph.query(
                    f"MERGE (n:Policy {{id:'{nid}'}}) "
                    f"SET n.policy_type='{pt}',n.title='{title}',"
                    f"    n.content='{content}',n.brand_id='{brand_id}',n.source_url='{src}' "
                    f"WITH n MATCH (b:Brand {{id:'{brand_id}'}}) MERGE (b)-[:HAS_POLICY {{type:'{pt}'}}]->(n)"
                )
            else:
                # Use the actual entity_type as node label — aligns with ontology schema.
                # e.g. SizeGuide items are stored as :SizeGuide nodes, not :KnowledgeItem.
                node_label = _valid_node_label(et)
                ext = item.get("extracted_at", datetime.now(timezone.utc).isoformat())
                graph.query(
                    f"MERGE (n:{node_label} {{id:'{nid}'}}) "
                    f"SET n.entity_type='{et}',n.title='{title}',"
                    f"    n.content='{content}',n.category='{cat}',"
                    f"    n.brand_id='{brand_id}',n.source_url='{src}',n.extracted_at='{ext}' "
                    f"WITH n MATCH (b:Brand {{id:'{brand_id}'}}) MERGE (b)-[:HAS_KNOWLEDGE]->(n)"
                )
            written += 1
    except Exception as e:
        log.error("graph_write_failed", brand_id=brand_id, error=str(e))
    return written


async def _write_pgvector(items: list[dict], brand_id: str) -> int:
    written = 0
    if not settings.postgres_host:
        return 0
    try:
        import psycopg
        conn_str = (
            f"host={settings.postgres_host} port={settings.postgres_port} "
            f"dbname={settings.postgres_db} user={settings.postgres_user} "
            f"password={settings.postgres_password}"
        )
        async with await psycopg.AsyncConnection.connect(conn_str) as conn:
            # Ensure table exists with the canonical schema (matches DB migration)
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_chunks ("
                "id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                "brand_id varchar(64) NOT NULL, chunk_type varchar(32), "
                "source_url text, content text NOT NULL, "
                "metadata jsonb DEFAULT '{}'::jsonb, "
                "crawled_at timestamptz DEFAULT now(), "
                "updated_at timestamptz DEFAULT now())"
            )
            for item in items:
                body = str(item.get("content", "")).strip()
                if not body:
                    continue
                await conn.execute(
                    "INSERT INTO knowledge_chunks "
                    "(brand_id, chunk_type, source_url, content, metadata) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        brand_id,
                        item.get("entity_type", "KnowledgeItem"),
                        item.get("source_url", ""),
                        body[:4000],
                        json.dumps({
                            "title": item.get("title", ""),
                            "category": item.get("category", ""),
                            "source_url": item.get("source_url", ""),
                            "keywords": item.get("keywords", []),
                        }),
                    ),
                )
                written += 1
            await conn.commit()
    except Exception as e:
        log.warning("pgvector_failed", brand_id=brand_id, error=str(e))
    return written


async def _apply_proposals(proposals: list[dict], brand_id: str) -> int:
    """
    Apply ontology proposals from node_ontology_review.

    Proposals with merge_action='add'   → propose new OntologyType
    Proposals with merge_action='merge' → enrich existing OntologyType
    """
    applied = 0
    if not proposals:
        return 0
    try:
        from core.ontology.dynamic import get_ontology_manager
        om = get_ontology_manager()
        seen: set[str] = set()
        for p in proposals:
            label = p.get("label", "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            action = p.get("merge_action", "add")

            if action == "merge":
                merged = om.merge_entity_type(
                    label=label,
                    new_properties=p.get("new_properties", []),
                    new_relationships=p.get("new_relationships", []),
                    enhanced_description=p.get("enhanced_description", ""),
                    evidence=p.get("evidence", ""),
                    brand_id=brand_id,
                )
                if merged:
                    applied += 1
            else:
                pid = om.propose_entity_type(
                    label=label,
                    description=p.get("description", ""),
                    properties=p.get("properties", []),
                    relationships=p.get("relationships", []),
                    brand_id=brand_id,
                    proposed_by="sub_agent",
                    evidence=p.get("evidence", ""),
                    auto_apply=True,
                )
                if pid != "exists":
                    applied += 1
    except Exception as e:
        log.warning("ontology_failed", error=str(e))
    return applied


async def node_synthesize(state: AgentCollectState) -> dict[str, Any]:
    items = state["extracted_items"]
    proposals = state["ontology_proposals"]
    brand_id = state["brand_id"]
    log.info("synthesis_start", brand_id=brand_id, items=len(items))

    ontology_applied = await _apply_proposals(proposals, brand_id)
    graph_n, pg_n = await asyncio.gather(
        _write_graph(items, brand_id),
        _write_pgvector(items, brand_id),
        return_exceptions=True,
    )
    if isinstance(graph_n, Exception):
        log.error("graph_err", e=str(graph_n)); graph_n = 0
    if isinstance(pg_n, Exception):
        log.warning("pg_err", e=str(pg_n)); pg_n = 0

    log.info(
        "synthesis_done",
        brand_id=brand_id,
        graph=graph_n,
        pg=pg_n,
        ontology=ontology_applied,
    )
    return {"phase": "done", "extracted_count": int(graph_n)}


# ═══════════════════════════════════════════════════════════════════════════
# NODE 3.5 — Ontology review: deduplicate + enrich proposals from subagents
# ═══════════════════════════════════════════════════════════════════════════

_BUILTIN_ENTITY_TYPES = {"FAQ", "Policy", "KnowledgeItem", "Review", "ProductCategory", "Brand", "IssueType"}

async def node_ontology_review(state: AgentCollectState) -> dict[str, Any]:
    """
    Ontology review agent — runs after collect, before synthesize.

    Three-way decision for each proposal:
    - APPROVED  → genuinely new entity type, add to ontology
    - MERGED    → label already exists, but this proposal adds new properties /
                  improves description → enrich the existing OntologyType
    - REJECTED  → alias of built-in, too vague, or adds nothing new

    Also requires agents to justify proposals with specific evidence so the
    architect can make informed decisions.
    """
    proposals = state["ontology_proposals"]
    if not proposals:
        return {}

    # Deduplicate by label, accumulating evidence across pages
    seen_labels: dict[str, dict] = {}
    for p in proposals:
        label = p.get("label", "").strip()
        if not label:
            continue
        if label not in seen_labels:
            seen_labels[label] = dict(p)  # copy so we can mutate
        else:
            existing = seen_labels[label]
            # Accumulate evidence from multiple page occurrences
            ev_parts = [existing.get("evidence", ""), p.get("evidence", "")]
            existing["evidence"] = " | ".join(x for x in ev_parts if x).strip(" |")
            # Union properties
            existing["properties"] = list(dict.fromkeys(
                existing.get("properties", []) + p.get("properties", [])
            ))

    raw_unique = list(seen_labels.values())
    if not raw_unique:
        return {}

    log.info("ontology_review_start", brand_id=state["brand_id"],
             raw_proposals=len(proposals), unique=len(raw_unique))

    non_builtin = [p for p in raw_unique if p.get("label") not in _BUILTIN_ENTITY_TYPES]
    if not non_builtin:
        log.info("ontology_review_all_builtin", brand_id=state["brand_id"])
        return {"ontology_proposals": []}

    # Load CURRENT active ontology types with their properties (for merge decisions)
    current_types: dict[str, dict] = {}
    try:
        from core.ontology.dynamic import get_ontology_manager
        for t in get_ontology_manager().get_entity_types(include_builtin=False):
            current_types[t["label"]] = t
    except Exception:
        pass

    llm = get_llm_client()
    proposals_json = json.dumps(non_builtin, indent=2)
    current_types_json = json.dumps(
        {k: {"description": v["description"], "properties": v["properties"]}
         for k, v in current_types.items()},
        indent=2,
    ) if current_types else "{}"

    prompt = textwrap.dedent(f"""
        You are an ontology architect for an e-commerce customer support knowledge graph.

        === FIXED BUILT-IN TYPES (cannot change, do not re-propose) ===
        {', '.join(sorted(_BUILTIN_ENTITY_TYPES))}

        === ACTIVE CUSTOM TYPES ALREADY IN THE SCHEMA ===
        {current_types_json}

        === PROPOSALS RAISED BY COLLECTION AGENTS ===
        {proposals_json}

        For each proposal decide ONE of:

        APPROVE — The label is genuinely new (not in built-ins or active types),
          represents a distinct e-commerce support entity, and is specific enough
          to be useful. Add it to the schema.

        MERGE — The label already exists in the active custom types above, but this
          proposal contains NEW properties not currently in the schema, or a better
          description. Enrich the existing type — do NOT add it as a new type.

        REJECT — Pure alias of a built-in or active type, too vague ("Content",
          "Page", "Item"), or adds no new information beyond what's already there.

        Return ONLY valid JSON (no markdown fences):
        {{
            "approved": [
                {{
                    "label": "DeliveryZone",
                    "description": "Geographic delivery zones with specific carrier rules and cut-off times",
                    "properties": ["brand_id", "zone_name", "carrier", "cutoff_time", "standard_days", "express_available"],
                    "relationships": [["Brand", "HAS_DELIVERY_ZONE", "incoming"]],
                    "evidence": "Page lists Northern Ireland surcharge of £5.95 and 3-5 day delivery vs 2-3 days mainland UK",
                    "merge_action": "add"
                }}
            ],
            "merged": [
                {{
                    "label": "SizeGuide",
                    "new_properties": ["chest_measurements_cm", "model_height"],
                    "new_relationships": [],
                    "enhanced_description": "Brand size guide tables including UK/EU/US conversions and body measurement ranges",
                    "evidence": "Page adds chest measurements in cm not present in current SizeGuide schema",
                    "merge_action": "merge"
                }}
            ],
            "rejected": [
                {{"label": "Policy", "reason": "alias of built-in Policy type"}}
            ]
        }}
    """).strip()

    try:
        raw = await llm.generate(prompt, max_tokens=3000, fast=False)
        m = re.search(r'\{[\s\S]+\}', raw)
        if m:
            data = json.loads(m.group(0))
            approved = data.get("approved", [])
            merged = data.get("merged", [])
            rejected = data.get("rejected", [])

            # Tag action field on each
            for p in approved:
                p["merge_action"] = "add"
            for p in merged:
                p["merge_action"] = "merge"

            log.info(
                "ontology_review_done",
                brand_id=state["brand_id"],
                approved=len(approved),
                merged=len(merged),
                rejected=len(rejected),
            )
            return {"ontology_proposals": approved + merged}
    except Exception as e:
        log.warning("ontology_review_failed", error=str(e))

    # Fallback: pass non-builtin proposals through as-is
    for p in non_builtin:
        p.setdefault("merge_action", "add")
    return {"ontology_proposals": non_builtin}


# ═══════════════════════════════════════════════════════════════════════════
# BRAND SCRAPING PROBE
# ═══════════════════════════════════════════════════════════════════════════

async def probe_site_access(base_url: str) -> dict[str, Any]:
    """
    Test what level of access we have to a site without full browser overhead.

    Returns a report with:
    - plain_httpx: can we GET the homepage with plain httpx?
    - content_rich: does the plain response have substantial content (>2000 chars)?
    - bot_protected: does the plain response look like a bot-gate page?
    - recommended_strategy: which fetch strategy to prefer for this site
    - response_chars: char count of the best plain response
    """
    report: dict[str, Any] = {
        "url": base_url,
        "plain_httpx": False,
        "content_rich": False,
        "bot_protected": False,
        "firecrawl_available": bool(settings.firecrawl_api_key),
        "recommended_strategy": "stealth",
        "response_chars": 0,
        "notes": [],
    }

    # Test 1: bare httpx (no UA header)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(base_url)
            if r.status_code < 400:
                report["plain_httpx"] = True
                report["response_chars"] = len(r.text)
                text_lower = r.text.lower()

                # Bot-gate signals
                bot_signals = ["access denied", "cloudflare", "just a moment", "enable javascript",
                               "captcha", "unusual traffic", "403 forbidden", "please verify"]
                if any(s in text_lower for s in bot_signals):
                    report["bot_protected"] = True
                    report["notes"].append("Bot-gate detected in plain response")
                elif len(r.text) > 2000:
                    report["content_rich"] = True

    except Exception as e:
        report["notes"].append(f"Plain httpx failed: {e}")

    # Test 2: httpx with browser UA
    try:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
        ) as client:
            r = await client.get(base_url)
            if r.status_code < 400 and len(r.text) > report["response_chars"]:
                report["response_chars"] = len(r.text)
                text_lower = r.text.lower()
                if not any(s in text_lower for s in ["access denied", "cloudflare", "just a moment", "captcha"]):
                    if len(r.text) > 2000:
                        report["content_rich"] = True
                        report["notes"].append("UA-spoofed httpx returns rich content — stealth browser may be unnecessary")
    except Exception:
        pass

    # Recommend strategy
    if report["content_rich"] and not report["bot_protected"]:
        report["recommended_strategy"] = "httpx"
        report["notes"].append("Site appears unprotected — plain httpx sufficient")
    elif settings.firecrawl_api_key:
        report["recommended_strategy"] = "firecrawl"
        report["notes"].append("Bot protection detected — FireCrawl recommended")
    else:
        report["recommended_strategy"] = "stealth"
        report["notes"].append("Bot protection detected — stealth browser required")

    return report


# ═══════════════════════════════════════════════════════════════════════════
# GRAPH CONSTRUCTION + PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def _build_graph() -> StateGraph:
    b = StateGraph(AgentCollectState)
    b.add_node("orchestrate", node_orchestrate)
    b.add_node("collect", node_collect)
    b.add_node("ontology_review", node_ontology_review)
    b.add_node("synthesize", node_synthesize)
    b.add_edge(START, "orchestrate")
    b.add_edge("orchestrate", "collect")
    b.add_edge("collect", "ontology_review")
    b.add_edge("ontology_review", "synthesize")
    b.add_edge("synthesize", END)
    return b.compile()


_graph = None


def _get_graph():
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


async def run_agent_collection(
    brand_id: str,
    brand_name: str,
    base_url: str,
    max_urls: int = _MAX_URLS_DEFAULT,
) -> dict[str, Any]:
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    base_url = base_url.rstrip("/")

    started_at = datetime.now(timezone.utc).isoformat()
    initial: AgentCollectState = {
        "brand_id": brand_id,
        "brand_name": brand_name,
        "base_url": base_url,
        "max_urls": max_urls,
        "entry_points": [],
        "extracted_items": [],
        "ontology_proposals": [],
        "phase": "discover",
        "errors": [],
        "extracted_count": 0,
        "started_at": started_at,
    }

    log.info("collection_start", brand_id=brand_id, base_url=base_url)
    t0 = time.monotonic()
    final = await _get_graph().ainvoke(initial)
    elapsed = round(time.monotonic() - t0, 1)

    log.info(
        "collection_complete",
        brand_id=brand_id,
        extracted=final.get("extracted_count", 0),
        pages=len(final.get("entry_points", [])),
        elapsed_s=elapsed,
    )
    return {
        "brand_id": brand_id,
        "brand_name": brand_name,
        "base_url": base_url,
        "phase": final.get("phase", "done"),
        "extracted_count": final.get("extracted_count", 0),
        "pages_visited": len(final.get("entry_points", [])),
        "ontology_proposals": len(final.get("ontology_proposals", [])),
        "errors": final.get("errors", []),
        "elapsed_s": elapsed,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# DOCUMENT INGESTION — PDF / DOCX / TXT / MD / HTML
# ═══════════════════════════════════════════════════════════════════════════

async def collect_from_documents(
    brand_id: str,
    brand_name: str,
    file_paths: list[str],
) -> dict[str, Any]:
    """
    Ingest brand-uploaded documents (PDF, DOCX, TXT, MD, HTML) into the
    knowledge graph and pgvector store using the same LLM extraction pipeline
    used for web-crawled pages.

    Each file is parsed into text chunks, each chunk is sent through
    _extract_chunk (same prompt as web pages), then ontology proposals are
    reviewed and all items are written to FalkorDB + pgvector.
    """
    from ingestion.doc_parser import parse_files, SUPPORTED_EXTENSIONS

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    llm = get_llm_client()

    from core.ontology.dynamic import get_ontology_manager
    om = get_ontology_manager()

    # ── Parse all files ──────────────────────────────────────────────────
    valid_paths = []
    skipped = []
    from pathlib import Path as _Path
    for p in file_paths:
        if not _Path(p).exists():
            skipped.append({"path": p, "reason": "file not found"})
            continue
        if _Path(p).suffix.lower() not in SUPPORTED_EXTENSIONS:
            skipped.append({"path": p, "reason": f"unsupported type: {_Path(p).suffix}"})
            continue
        valid_paths.append(p)

    if not valid_paths:
        return {
            "brand_id": brand_id,
            "extracted_count": 0,
            "files_processed": 0,
            "skipped": skipped,
            "errors": ["No valid files to process"],
        }

    log.info("doc_ingest_start", brand_id=brand_id, files=len(valid_paths))
    chunks = parse_files(valid_paths)

    # ── Extract items from each chunk via LLM ─────────────────────────────
    all_items: list[dict] = []
    all_proposals: list[dict] = []
    errors: list[str] = []

    ontology_labels = om.get_type_labels_for_prompt()

    # Process in batches of 4 to avoid hammering the LLM
    batch_size = 4
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        tasks = []
        for chunk_data in batch:
            source_label = chunk_data["source"]
            if chunk_data.get("page"):
                source_label += f"#page{chunk_data['page']}"
            tasks.append(
                _extract_chunk(
                    llm=llm,
                    url=f"file://{source_label}",
                    category="document",
                    chunk=chunk_data["text"],
                    brand_id=brand_id,
                    brand_name=brand_name,
                    ontology_labels=ontology_labels,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                errors.append(str(r))
            else:
                items, proposals = r
                all_items.extend(items)
                all_proposals.extend(proposals)

    # ── Ontology review (same as web pipeline) ────────────────────────────
    if all_proposals:
        # Deduplicate by label
        seen_labels: dict[str, dict] = {}
        for p in all_proposals:
            lbl = p.get("label", "")
            if lbl:
                if lbl not in seen_labels:
                    seen_labels[lbl] = p
                else:
                    # Merge evidence
                    existing = seen_labels[lbl]
                    existing["evidence"] = (
                        (existing.get("evidence") or "") + " | " + (p.get("evidence") or "")
                    )[:500]
        deduped_proposals = list(seen_labels.values())

        # Quick pass: tag non-builtin proposals for apply
        for p in deduped_proposals:
            p.setdefault("merge_action", "add")

        await _apply_proposals(deduped_proposals, brand_id)

    # ── Write to stores ───────────────────────────────────────────────────
    for item in all_items:
        item.setdefault("source_url", f"doc://{brand_id}")

    graph_n, pg_n = await asyncio.gather(
        _write_graph(all_items, brand_id),
        _write_pgvector(all_items, brand_id),
        return_exceptions=True,
    )
    if isinstance(graph_n, Exception):
        errors.append(f"graph write: {graph_n}"); graph_n = 0
    if isinstance(pg_n, Exception):
        errors.append(f"pgvector write: {pg_n}"); pg_n = 0

    elapsed = round(time.monotonic() - t0, 1)
    log.info(
        "doc_ingest_complete",
        brand_id=brand_id,
        files=len(valid_paths),
        chunks=len(chunks),
        items=len(all_items),
        graph=graph_n,
        pg=pg_n,
        elapsed_s=elapsed,
    )

    return {
        "brand_id": brand_id,
        "files_processed": len(valid_paths),
        "chunks_parsed": len(chunks),
        "extracted_count": int(graph_n),
        "pgvector_count": int(pg_n),
        "ontology_proposals": len(all_proposals),
        "skipped": skipped,
        "errors": errors,
        "elapsed_s": elapsed,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_collection_status(brand_id: str) -> dict[str, Any] | None:
    try:
        from core.memory.session import get_redis
        r = get_redis()
        raw = await r.get(f"agent_collect:{brand_id}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _save_status(brand_id: str, status: dict[str, Any]) -> None:
    try:
        from core.memory.session import get_redis
        r = get_redis()
        await r.setex(f"agent_collect:{brand_id}", 86400, json.dumps(status))
    except Exception:
        pass
