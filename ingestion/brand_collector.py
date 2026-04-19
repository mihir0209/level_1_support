"""
Brand data collection orchestrator — fully open-source, no paid APIs required.

Tools (in preference order):
  1. crawl4ai         — async JS-capable crawler (BFS + LLM extraction)
  2. ScrapeGraphAI    — multi-URL structured LLM extraction
  3. httpx + LLM      — plain HTTP fetch → LLM extraction (always available fallback)

Discovery uses DuckDuckGo HTML (https://html.duckduckgo.com/html/) — free, no API key.

Collection flow:
  SEED     → crawl4ai BFS on brand domain (or seed list if crawl4ai unavailable)
  DISCOVER → DuckDuckGo HTML scrape for news / reviews / social
  FETCH    → ScrapeGraphAI batch + crawl4ai single + httpx+LLM fallback
  DONE     → all URLs processed

Resume logic:
  - resume=True  + phase IN (SEED, DISCOVER, FETCH) → continue from saved state
  - resume=True  + phase == DONE                     → restart fresh (re-collect)
  - resume=False                                     → wipe Redis state + dedup, restart

Deduplication:
  Redis SET  brand:collect:{id}:visited_urls   → SHA256(canonical URL)
  Redis HASH brand:collect:{id}:content_hashes → SHA256(content) → URL

Checkpoint: CollectState saved to Redis as JSON after every batch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import quote_plus, urlparse, urlunparse

import httpx
import structlog

from config import settings

log = structlog.get_logger(__name__)


# ── Redis key helpers ─────────────────────────────────────────────────────────
def _rkey(brand_id: str, suffix: str) -> str:
    return f"brand:collect:{brand_id}:{suffix}"


# ── URL helpers ───────────────────────────────────────────────────────────────
def canonical_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))
    except Exception:
        return url.strip()


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


# ── Phases ────────────────────────────────────────────────────────────────────
class CollectPhase(str, Enum):
    SEED = "seed"
    DISCOVER = "discover"
    FETCH = "fetch"
    DONE = "done"
    FAILED = "failed"


@dataclass
class CollectState:
    brand_id: str
    phase: CollectPhase = CollectPhase.SEED
    pending_urls: list[str] = field(default_factory=list)
    visited_count: int = 0
    extracted_count: int = 0
    error_count: int = 0
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase"] = self.phase.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CollectState":
        d = dict(d)
        d["phase"] = CollectPhase(d.get("phase", "seed"))
        return cls(**d)


# ── Extraction schema ─────────────────────────────────────────────────────────
BRAND_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {
            "type": "string",
            "enum": ["policy", "faq", "about", "product", "news", "review", "social", "other"],
        },
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative", "mixed"]},
        "faq_pairs": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "question": {"type": "string"},
                "answer": {"type": "string"},
            }},
        },
        "policy_details": {
            "type": "object",
            "properties": {
                "policy_type": {"type": "string"},
                "window_days": {"type": "integer"},
                "conditions": {"type": "array", "items": {"type": "string"}},
            },
        },
        "review_data": {
            "type": "object",
            "properties": {
                "rating": {"type": "number"},
                "platform": {"type": "string"},
                "review_count": {"type": "integer"},
                "common_complaints": {"type": "array", "items": {"type": "string"}},
                "common_praises": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "required": ["page_type", "summary", "key_facts"],
}

_SCHEMA_STR = json.dumps(BRAND_PAGE_SCHEMA, indent=2)

# ── DuckDuckGo dork queries ───────────────────────────────────────────────────
def _dork_queries(brand_name: str, domain: str) -> list[str]:
    return [
        f'"{brand_name}" customer reviews 2024 2025',
        f'"{brand_name}" return policy refund shipping',
        f'site:trustpilot.com "{brand_name}"',
        f'site:reddit.com "{brand_name}" customer service',
        f'site:reviews.io OR site:resellerratings.com "{brand_name}"',
        f'"{brand_name}" complaint OR delivery problem',
        f'"{brand_name}" news latest',
    ]


# ══════════════════════════════════════════════════════════════════════════════
# DEDUP + CHECKPOINT
# ══════════════════════════════════════════════════════════════════════════════

class DedupTracker:
    def __init__(self, brand_id: str, redis_client):
        self._r = redis_client
        self._url_key = _rkey(brand_id, "visited_urls")
        self._content_key = _rkey(brand_id, "content_hashes")
        self._brand_id = brand_id

    async def is_url_seen(self, url: str) -> bool:
        return bool(await self._r.sismember(self._url_key, url_hash(url)))

    async def mark_url_seen(self, url: str) -> None:
        await self._r.sadd(self._url_key, url_hash(url))

    async def is_content_duplicate(self, text: str) -> bool:
        return bool(await self._r.hexists(self._content_key, content_hash(text)))

    async def mark_content_seen(self, text: str, url: str) -> None:
        await self._r.hset(self._content_key, content_hash(text), url)

    async def visited_count(self) -> int:
        return await self._r.scard(self._url_key)

    async def clear(self) -> None:
        await self._r.delete(self._url_key, self._content_key)


class CheckpointManager:
    def __init__(self, brand_id: str, redis_client):
        self._r = redis_client
        self._key = _rkey(brand_id, "state")

    async def save(self, state: CollectState) -> None:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        await self._r.set(self._key, json.dumps(state.to_dict()), ex=86400 * 30)

    async def load(self, brand_id: str) -> CollectState | None:
        raw = await self._r.get(self._key)
        if not raw:
            return None
        try:
            return CollectState.from_dict(json.loads(raw))
        except Exception as e:
            log.warning("checkpoint_load_failed", brand_id=brand_id, error=str(e))
            return None

    async def clear(self) -> None:
        await self._r.delete(self._key)


# ══════════════════════════════════════════════════════════════════════════════
# LLM HELPER — works with any configured provider via litellm
# ══════════════════════════════════════════════════════════════════════════════

def _litellm_model() -> tuple[str, str]:
    """Return (model_string, api_key) for litellm routing."""
    if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        return settings.primary_model, settings.anthropic_api_key
    elif settings.llm_provider == "azure" and settings.azure_openai_api_key:
        return f"azure/{settings.azure_primary_deployment}", settings.azure_openai_api_key
    else:
        return settings.openai_primary_model, settings.openai_api_key


async def _llm_extract(text: str, brand_name: str, url: str) -> dict | None:
    """
    Direct LLM extraction from raw text — the always-available fallback.
    Uses litellm so it works with Anthropic, Azure, or OpenAI.
    """
    if not text or len(text.strip()) < 50:
        return None
    try:
        import litellm

        model, api_key = _litellm_model()
        prompt = (
            f"You are extracting structured data from a web page about the company '{brand_name}'.\n"
            f"URL: {url}\n\n"
            f"PAGE CONTENT (truncated to 4000 chars):\n{text[:4000]}\n\n"
            f"Extract and return ONLY valid JSON matching this schema:\n{_SCHEMA_STR}\n\n"
            "Rules:\n"
            "- page_type: classify as policy/faq/review/social/news/other\n"
            "- summary: 2-3 sentences describing the page\n"
            "- key_facts: 3-10 important facts from the page\n"
            "- faq_pairs: only if the page has Q&A content\n"
            "- policy_details: only if page describes a policy (returns, shipping, etc.)\n"
            "- review_data: only if page has reviews or ratings\n"
            "Return ONLY the JSON object, no markdown fences."
        )

        resp = await litellm.acompletion(
            model=model,
            api_key=api_key,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
    except Exception as e:
        log.warning("llm_extract_failed", url=url, error=str(e))
        return None


# ══════════════════════════════════════════════════════════════════════════════
# DISCOVERY — DuckDuckGo HTML (no API key)
# ══════════════════════════════════════════════════════════════════════════════

async def _discover_via_duckduckgo(queries: list[str], max_per_query: int = 6) -> list[str]:
    """
    Scrape DuckDuckGo HTML search pages to discover external URLs.
    Tries crawl4ai first (handles JS better); falls back to plain httpx.
    """
    discovered: list[str] = []

    # Try crawl4ai
    crawl4ai_ok = False
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
        crawl4ai_ok = True

        async with AsyncWebCrawler() as crawler:
            for query in queries:
                ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
                result = await crawler.arun(
                    url=ddg_url,
                    config=CrawlerRunConfig(
                        cache_mode=CacheMode.BYPASS,
                        word_count_threshold=0,
                    ),
                )
                if not result.success:
                    continue
                links = result.links or {}
                for link in (links.get("external") or [])[:max_per_query]:
                    href = link.get("href") if isinstance(link, dict) else str(link)
                    if href and href.startswith("http") and "duckduckgo" not in href:
                        discovered.append(href)
    except ImportError:
        pass
    except Exception as e:
        log.warning("crawl4ai_ddg_failed", error=str(e))

    # Plain httpx fallback
    if not crawl4ai_ok or not discovered:
        try:
            from html.parser import HTMLParser

            class _LinkParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.links: list[str] = []

                def handle_starttag(self, tag, attrs):
                    if tag == "a":
                        attrs_dict = dict(attrs)
                        href = attrs_dict.get("href", "")
                        if href.startswith("http") and "duckduckgo" not in href:
                            self.links.append(href)

            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                for query in queries:
                    try:
                        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
                        resp = await client.get(
                            ddg_url,
                            headers={
                                "User-Agent": "Mozilla/5.0 (compatible; L1SupportBot/1.0)",
                                "Accept": "text/html",
                            },
                        )
                        parser = _LinkParser()
                        parser.feed(resp.text)
                        discovered.extend(parser.links[:max_per_query])
                    except Exception as e:
                        log.warning("httpx_ddg_query_failed", query=query[:40], error=str(e))
        except Exception as e:
            log.warning("httpx_ddg_discovery_failed", error=str(e))

    return list(dict.fromkeys(discovered))


# ══════════════════════════════════════════════════════════════════════════════
# BRAND DOMAIN CRAWL — crawl4ai BFS (falls back to seed list)
# ══════════════════════════════════════════════════════════════════════════════

async def _deep_crawl_domain(base_url: str, max_pages: int = 40) -> list[str]:
    """BFS crawl of the brand's own domain using crawl4ai.
    base_url must be a fully-qualified URL (with scheme).
    """
    from urllib.parse import urljoin
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
        from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

        strategy = BFSDeepCrawlStrategy(
            max_depth=3,
            max_pages=max_pages,
        )
        collected: list[str] = []
        async with AsyncWebCrawler() as crawler:
            results = await crawler.arun(
                url=base_url,
                config=CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    deep_crawl_strategy=strategy,
                    word_count_threshold=30,
                ),
            )
            for r in (results if isinstance(results, list) else [results]):
                if r and r.success and r.url:
                    collected.append(r.url)
        return list(dict.fromkeys(collected))

    except ImportError:
        log.info("crawl4ai_unavailable", hint="pip install crawl4ai; using seed list fallback")
    except Exception as e:
        log.warning("deep_crawl_failed", domain=base_url, error=str(e))

    # Fallback: known support path patterns
    paths = ["/", "/help", "/faq", "/returns", "/delivery", "/shipping",
             "/contact", "/about", "/customer-service", "/pages/faq",
             "/pages/returns", "/pages/delivery-information"]
    return [urljoin(base_url, p) for p in paths]


# ══════════════════════════════════════════════════════════════════════════════
# EXTRACTION — cascade: ScrapeGraphAI → crawl4ai → httpx+LLM
# ══════════════════════════════════════════════════════════════════════════════

async def _extract_batch_scrapegraph(urls: list[str], brand_name: str) -> list[tuple[str, dict]]:
    """ScrapeGraphAI multi-URL batch extraction."""
    if not urls:
        return []
    try:
        from scrapegraphai.graphs import SmartScraperMultiGraph

        # ScrapeGraphAI uses openai-compatible config; for Azure supply base_url + api_version
        if settings.llm_provider == "azure" and settings.azure_openai_api_key:
            sg_config = {
                "llm": {
                    "model": f"azure_openai/{settings.azure_primary_deployment}",
                    "api_key": settings.azure_openai_api_key,
                    "azure_endpoint": settings.azure_openai_endpoint,
                    "api_version": settings.azure_openai_api_version,
                },
                "headless": True,
                "verbose": False,
            }
        else:
            model, api_key = _litellm_model()
            sg_config = {"llm": {"model": model, "api_key": api_key}, "headless": True, "verbose": False}
        prompt = (
            f"For each page about {brand_name}, extract: page_type (policy/faq/review/social/news/other), "
            "summary, key_facts, faq_pairs if present, policy_details if present, "
            "review_data if present (rating, platform, complaints, praises), sentiment."
        )
        graph = SmartScraperMultiGraph(prompt=prompt, source=urls, config=sg_config)

        # graph.run() uses asyncio.run() internally — run in a thread to avoid
        # conflict with FastAPI's event loop. Suppress its internal stderr noise
        # (LangChain JSON parse errors when the LLM returns NA instead of null).
        import contextlib, io

        def _run() -> object:
            with contextlib.redirect_stderr(io.StringIO()):
                return graph.run()

        result = await asyncio.to_thread(_run)

        if isinstance(result, dict):
            return [(u, result[u]) for u in urls if u in result]
        if isinstance(result, list):
            return list(zip(urls, result))
    except ImportError:
        pass
    except Exception as e:
        log.warning("scrapegraph_batch_failed", count=len(urls), error=str(e))
    return []


async def _extract_single_crawl4ai(url: str, brand_name: str) -> dict | None:
    """crawl4ai LLM extraction for a single URL."""
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
        from crawl4ai.extraction_strategy import LLMExtractionStrategy, LLMConfig

        model, api_key = _litellm_model()
        provider = (
            f"anthropic/{model}" if settings.llm_provider == "anthropic"
            else f"azure/{model}" if settings.llm_provider == "azure"
            else f"openai/{model}"
        )
        strategy = LLMExtractionStrategy(
            llm_config=LLMConfig(provider=provider, api_token=api_key),
            schema=BRAND_PAGE_SCHEMA,
            instruction=f"Extract support-relevant info about {brand_name}. Return exact JSON schema.",
        )
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                url=url,
                config=CrawlerRunConfig(
                    extraction_strategy=strategy,
                    cache_mode=CacheMode.BYPASS,
                    word_count_threshold=50,
                    wait_for="body",
                ),
            )
        if not result.success:
            return None
        extracted = result.extracted_content
        if isinstance(extracted, str):
            try:
                extracted = json.loads(extracted)
            except Exception:
                return None
        return extracted[0] if isinstance(extracted, list) else extracted
    except ImportError:
        pass
    except TypeError as e:
        # ForwardRef Pydantic v2 compat bug in installed crawl4ai — always falls through
        log.debug("crawl4ai_single_skipped", url=url, reason=str(e))
    except Exception as e:
        log.warning("crawl4ai_single_failed", url=url, error=str(e))
    return None


async def _extract_httpx_llm(url: str, brand_name: str) -> dict | None:
    """
    Always-available fallback: fetch with httpx, extract with LLM.
    Strips HTML tags before passing to the LLM to save tokens.
    """
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; L1SupportBot/1.0)"},
            )
            resp.raise_for_status()
    except Exception as e:
        log.warning("httpx_fetch_failed", url=url, error=str(e))
        return None

    # Strip HTML to plain text
    try:
        from html.parser import HTMLParser

        class _TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self._chunks: list[str] = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "nav", "footer", "header"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "footer", "header"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip and data.strip():
                    self._chunks.append(data.strip())

            @property
            def text(self) -> str:
                return " ".join(self._chunks)

        extractor = _TextExtractor()
        extractor.feed(resp.text)
        text = extractor.text
    except Exception:
        text = resp.text

    return await _llm_extract(text, brand_name, url)


async def _extract_one(url: str, brand_name: str) -> dict | None:
    """
    Cascade: ScrapeGraphAI(1-URL) → crawl4ai → httpx+LLM.
    Single-URL wrapper used in the per-URL fallback path.
    """
    result = await _extract_single_crawl4ai(url, brand_name)
    if result:
        return result
    return await _extract_httpx_llm(url, brand_name)


# ══════════════════════════════════════════════════════════════════════════════
# STORAGE
# ══════════════════════════════════════════════════════════════════════════════

async def _write_to_graph(brand_id: str, url: str, extracted: dict) -> None:
    try:
        from core.ontology.schema import get_graph_client
        _, graph = get_graph_client()

        page_type = extracted.get("page_type", "other")
        summary = (extracted.get("summary") or "")[:500].replace("'", "")
        source_hash = url_hash(url)

        if page_type == "faq":
            for pair in (extracted.get("faq_pairs") or []):
                q = (pair.get("question") or "")[:400].replace("'", "")
                a = (pair.get("answer") or "")[:800].replace("'", "")
                if not q:
                    continue
                faq_id = f"{brand_id}_{hashlib.sha256(q.encode()).hexdigest()[:12]}"
                graph.query(
                    f"MERGE (f:FAQ {{faq_id: '{faq_id}'}}) "
                    f"SET f.brand_id = '{brand_id}', f.question = '{q}', "
                    f"    f.answer = '{a}', f.source_url = '{url}', f.category = 'collected' "
                    f"WITH f MATCH (b:Brand {{brand_id: '{brand_id}'}}) "
                    f"MERGE (b)-[:HAS_FAQ]->(f)"
                )

        elif page_type == "policy":
            pd = extracted.get("policy_details") or {}
            ptype = (pd.get("policy_type") or "general").replace("'", "")
            window = pd.get("window_days") or 0
            conditions = json.dumps(pd.get("conditions") or []).replace("'", "")
            policy_id = f"{brand_id}_{ptype}_{source_hash[:8]}"
            graph.query(
                f"MERGE (p:Policy {{policy_id: '{policy_id}'}}) "
                f"SET p.brand_id = '{brand_id}', p.policy_type = '{ptype}', "
                f"    p.title = '{ptype.replace('_', ' ').title()}', "
                f"    p.summary = '{summary}', p.return_window_days = {window}, "
                f"    p.conditions = '{conditions}', p.source_url = '{url}' "
                f"WITH p MATCH (b:Brand {{brand_id: '{brand_id}'}}) "
                f"MERGE (b)-[:HAS_POLICY]->(p)"
            )

        elif page_type in ("review", "social", "news"):
            sentiment = (extracted.get("sentiment") or "neutral").replace("'", "")
            rd = extracted.get("review_data") or {}
            rating = rd.get("rating") or 0.0
            platform = (rd.get("platform") or urlparse(url).netloc).replace("'", "")
            complaints = json.dumps(rd.get("common_complaints") or []).replace("'", "")
            praises = json.dumps(rd.get("common_praises") or []).replace("'", "")
            mention_id = f"{brand_id}_{page_type}_{source_hash[:10]}"
            graph.query(
                f"MERGE (m:BrandMention {{mention_id: '{mention_id}'}}) "
                f"SET m.brand_id = '{brand_id}', m.type = '{page_type}', "
                f"    m.sentiment = '{sentiment}', m.platform = '{platform}', "
                f"    m.rating = {rating}, m.summary = '{summary}', "
                f"    m.complaints = '{complaints}', m.praises = '{praises}', "
                f"    m.source_url = '{url}' "
                f"WITH m MATCH (b:Brand {{brand_id: '{brand_id}'}}) "
                f"MERGE (b)-[:HAS_MENTION]->(m)"
            )

        else:
            item_id = f"{brand_id}_ki_{source_hash[:10]}"
            facts = json.dumps(extracted.get("key_facts") or []).replace("'", "")
            graph.query(
                f"MERGE (k:KnowledgeItem {{item_id: '{item_id}'}}) "
                f"SET k.brand_id = '{brand_id}', k.page_type = '{page_type}', "
                f"    k.summary = '{summary}', k.key_facts = '{facts}', "
                f"    k.source_url = '{url}' "
                f"WITH k MATCH (b:Brand {{brand_id: '{brand_id}'}}) "
                f"MERGE (b)-[:HAS_KNOWLEDGE]->(k)"
            )
    except Exception as e:
        log.warning("graph_write_failed", url=url, error=str(e))


async def _write_to_pgvector(brand_id: str, url: str, extracted: dict) -> None:
    try:
        import psycopg
        parts = [extracted.get("summary") or ""]
        parts += [f"Q: {p['question']}\nA: {p['answer']}" for p in (extracted.get("faq_pairs") or [])]
        parts += [str(f) for f in (extracted.get("key_facts") or [])]
        content = "\n".join(parts).strip()
        if len(content) < 30:
            return
        conn = psycopg.connect(
            host=settings.postgres_host, port=settings.postgres_port,
            dbname=settings.postgres_db, user=settings.postgres_user,
            password=settings.postgres_password,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO knowledge_chunks (brand_id, chunk_type, content, metadata) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (brand_id, extracted.get("page_type", "collected"),
                 content[:8000], json.dumps({"source_url": url, "content_hash": content_hash(content)})),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("pgvector_write_failed", url=url, error=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

async def collect_brand_data(
    brand_id: str,
    brand_name: str,
    base_domain: str,
    resume: bool = True,
    max_urls: int = 200,
    concurrency: int = 5,
    scrapegraph_batch_size: int = 8,
) -> dict[str, Any]:
    """
    Full brand collection pipeline. Always makes progress — httpx+LLM fallback
    means it works even without crawl4ai or ScrapeGraphAI installed.
    """
    from core.memory.session import get_redis
    r = get_redis()
    dedup = DedupTracker(brand_id, r)
    ckpt = CheckpointManager(brand_id, r)

    # Normalize base_domain → always a full URL with scheme
    if not base_domain.startswith(("http://", "https://")):
        base_domain = f"https://{base_domain}"
    base_domain = base_domain.rstrip("/")

    # ── Resume logic ─────────────────────────────────────────────────────────
    state: CollectState | None = None

    if not resume:
        # Explicit fresh start — wipe all previous state
        await ckpt.clear()
        await dedup.clear()
        log.info("collection_fresh_start", brand_id=brand_id)
    else:
        state = await ckpt.load(brand_id)
        if state:
            if state.phase == CollectPhase.DONE:
                # Previous run completed — treat a re-trigger as "collect again fresh"
                log.info("collection_restarted", brand_id=brand_id, reason="previous_run_was_complete")
                await ckpt.clear()
                await dedup.clear()
                state = None
            else:
                log.info("collection_resumed", brand_id=brand_id, phase=state.phase, pending=len(state.pending_urls))

    if not state:
        state = CollectState(brand_id=brand_id)
        log.info("collection_started", brand_id=brand_id, domain=base_domain)

    t_start = time.monotonic()

    # ── SEED ─────────────────────────────────────────────────────────────────
    if state.phase == CollectPhase.SEED:
        log.info("phase_seed", brand_id=brand_id, domain=base_domain)
        domain_urls = await _deep_crawl_domain(base_domain, max_pages=40)
        for u in domain_urls:
            if u not in state.pending_urls:
                state.pending_urls.append(u)
        state.phase = CollectPhase.DISCOVER
        await ckpt.save(state)
        log.info("seed_done", brand_id=brand_id, queued=len(state.pending_urls))

    # ── DISCOVER ──────────────────────────────────────────────────────────────
    if state.phase == CollectPhase.DISCOVER:
        log.info("phase_discover", brand_id=brand_id)
        queries = _dork_queries(brand_name, base_domain)
        found = await _discover_via_duckduckgo(queries, max_per_query=6)
        new_count = 0
        for u in found:
            if u not in state.pending_urls and not await dedup.is_url_seen(u):
                state.pending_urls.append(u)
                new_count += 1
        state.phase = CollectPhase.FETCH
        await ckpt.save(state)
        log.info("discover_done", brand_id=brand_id, new_urls=new_count, total_queued=len(state.pending_urls))

    # ── FETCH ─────────────────────────────────────────────────────────────────
    if state.phase == CollectPhase.FETCH:
        log.info("phase_fetch", brand_id=brand_id, queue_size=len(state.pending_urls))
        queue = [u for u in state.pending_urls if not await dedup.is_url_seen(u)]
        state.pending_urls = []
        processed = 0

        while queue and processed < max_urls:
            batch = queue[:scrapegraph_batch_size]
            queue = queue[scrapegraph_batch_size:]

            # Mark visited immediately
            for u in batch:
                await dedup.mark_url_seen(u)

            # 1. Try ScrapeGraphAI batch
            sg_results = await _extract_batch_scrapegraph(batch, brand_name)
            sg_handled = {url for url, _ in sg_results}

            # 2. Per-URL fallback for anything ScrapeGraphAI didn't handle
            sem = asyncio.Semaphore(concurrency)

            async def _process_fallback(url: str) -> tuple[str, dict | None]:
                async with sem:
                    return url, await _extract_one(url, brand_name)

            fallback_urls = [u for u in batch if u not in sg_handled]
            fallback_results = await asyncio.gather(*[_process_fallback(u) for u in fallback_urls])

            all_results: list[tuple[str, dict | None]] = sg_results + list(fallback_results)

            for url, extracted in all_results:
                if not extracted:
                    state.error_count += 1
                    log.debug("extract_failed", url=url)
                    continue
                raw = json.dumps(extracted)
                if await dedup.is_content_duplicate(raw):
                    continue
                await dedup.mark_content_seen(raw, url)
                await asyncio.gather(
                    _write_to_graph(brand_id, url, extracted),
                    _write_to_pgvector(brand_id, url, extracted),
                )
                state.extracted_count += 1
                log.info("extracted", brand_id=brand_id, url=url, type=extracted.get("page_type"))

            state.visited_count += len(batch)
            processed += len(batch)
            await ckpt.save(state)

        state.phase = CollectPhase.DONE
        await ckpt.save(state)

    elapsed = round(time.monotonic() - t_start, 1)
    log.info(
        "collection_complete",
        brand_id=brand_id,
        phase=state.phase.value,
        visited=state.visited_count,
        extracted=state.extracted_count,
        errors=state.error_count,
        elapsed_s=elapsed,
    )
    return {
        "brand_id": brand_id,
        "phase": state.phase.value,
        "visited_urls": state.visited_count,
        "extracted_items": state.extracted_count,
        "errors": state.error_count,
        "elapsed_seconds": elapsed,
        "can_resume": state.phase not in (CollectPhase.DONE, CollectPhase.FAILED),
    }


async def get_collection_status(brand_id: str) -> dict | None:
    from core.memory.session import get_redis
    r = get_redis()
    state = await CheckpointManager(brand_id, r).load(brand_id)
    if not state:
        return None
    visited = await DedupTracker(brand_id, r).visited_count()
    return {
        "brand_id": brand_id,
        "phase": state.phase.value,
        "visited_urls": visited,
        "extracted_count": state.extracted_count,
        "error_count": state.error_count,
        "started_at": state.started_at,
        "updated_at": state.updated_at,
        "pending_queue_length": len(state.pending_urls),
    }
