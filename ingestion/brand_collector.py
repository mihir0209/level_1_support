"""
Brand data collection orchestrator — fully open-source, no paid APIs required.

Tools used:
  • crawl4ai   — async web crawler (JS-capable, handles any URL including search pages)
  • ScrapeGraphAI — multi-URL structured LLM extraction
  • DuckDuckGo HTML search (https://html.duckduckgo.com/html/) — free, no API key

Collection flow per brand:
  1. SEED      — queue the brand's own support pages + sitemap
  2. DISCOVER  — crawl DuckDuckGo HTML for news / reviews / social via dork queries
  3. FETCH     — crawl4ai deep-crawls brand domain (BFS, max_depth=3)
                 ScrapeGraphAI extracts structured data from all discovered URLs
  4. STORE     — write typed data to FalkorDB graph + Postgres pgvector
  5. CHECKPOINT — persist state to Redis after every batch; resume on next call

Deduplication:
  Redis SET  brand:collect:{brand_id}:visited_urls   → SHA256(canonical URL)
  Redis HASH brand:collect:{brand_id}:content_hashes → SHA256(content) → URL

Schema covers: policy · faq · review · social · news · other
Reviews and social → BrandMention nodes with sentiment + complaints + praises
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse, urlunparse, quote_plus

import structlog

from config import settings

log = structlog.get_logger(__name__)


# ── Redis key helpers ─────────────────────────────────────────────────────────
def _rkey(brand_id: str, suffix: str) -> str:
    return f"brand:collect:{brand_id}:{suffix}"


# ── URL canonicalization + hashing ────────────────────────────────────────────
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


# ── Phase enumeration ─────────────────────────────────────────────────────────
class CollectPhase(str, Enum):
    SEED = "seed"
    DISCOVER = "discover"
    FETCH = "fetch"
    DONE = "done"
    FAILED = "failed"


# ── Checkpoint state ──────────────────────────────────────────────────────────
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


# ── Extraction schema for crawl4ai LLM strategy + ScrapeGraphAI ──────────────
BRAND_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {
            "type": "string",
            "enum": ["policy", "faq", "about", "product", "news", "review", "social", "other"],
        },
        "brand_name": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "sentiment": {
            "type": "string",
            "enum": ["positive", "neutral", "negative", "mixed"],
        },
        "faq_pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                },
            },
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

# ── DuckDuckGo dork queries for multi-source discovery ────────────────────────
def _dork_queries(brand_name: str, domain: str) -> list[str]:
    return [
        f'"{brand_name}" customer reviews',
        f'"{brand_name}" return policy shipping',
        f'site:trustpilot.com "{brand_name}"',
        f'site:reddit.com "{brand_name}" customer service',
        f'"{brand_name}" news 2024 2025',
        f'site:reviews.io "{brand_name}"',
        f'"{brand_name}" complaint OR refund OR delivery problem',
    ]


# ══════════════════════════════════════════════════════════════════════════════
# DEDUP TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class DedupTracker:
    def __init__(self, brand_id: str, redis_client):
        self._r = redis_client
        self._url_key = _rkey(brand_id, "visited_urls")
        self._content_key = _rkey(brand_id, "content_hashes")

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


# ══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT MANAGER
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# DISCOVERY — crawl4ai scrapes DuckDuckGo HTML (free, no API key)
# ══════════════════════════════════════════════════════════════════════════════

async def _discover_via_duckduckgo(queries: list[str], max_per_query: int = 6) -> list[str]:
    """
    Crawl DuckDuckGo HTML search pages with crawl4ai to extract result URLs.
    Uses https://html.duckduckgo.com/html/ — no JS, no API key required.
    """
    discovered: list[str] = []
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

        async with AsyncWebCrawler() as crawler:
            for query in queries:
                ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
                result = await crawler.arun(
                    url=ddg_url,
                    config=CrawlerRunConfig(
                        cache_mode=CacheMode.BYPASS,
                        word_count_threshold=0,
                        css_selector="a.result__a",
                    ),
                )
                if not result.success:
                    continue
                # Extract external links from the result page
                external_links = result.links.get("external", []) if result.links else []
                for link in external_links[:max_per_query]:
                    href = link.get("href") if isinstance(link, dict) else str(link)
                    if href and href.startswith("http"):
                        discovered.append(href)

    except ImportError:
        log.warning("crawl4ai_not_installed", hint="pip install crawl4ai")
    except Exception as e:
        log.warning("duckduckgo_discovery_failed", error=str(e))

    return list(dict.fromkeys(discovered))


# ══════════════════════════════════════════════════════════════════════════════
# DEEP CRAWL — crawl4ai BFS from brand's own domain
# ══════════════════════════════════════════════════════════════════════════════

async def _deep_crawl_domain(base_domain: str, max_pages: int = 40) -> list[str]:
    """
    BFS deep-crawl the brand's own domain with crawl4ai.
    Discovers all internal support/FAQ/policy/help pages automatically.
    """
    collected_urls: list[str] = []
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
        from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

        strategy = BFSDeepCrawlStrategy(
            max_depth=3,
            max_pages=max_pages,
            include_patterns=[
                r".*(help|faq|support|returns?|refund|delivery|shipping|policy|contact|about).*"
            ],
            exclude_patterns=[
                r".*(checkout|cart|login|register|cdn|\.jpg|\.png|\.css|\.js).*"
            ],
        )

        async with AsyncWebCrawler() as crawler:
            results = await crawler.arun(
                url=f"https://{base_domain}/",
                config=CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    deep_crawl_strategy=strategy,
                    word_count_threshold=30,
                ),
            )
            if isinstance(results, list):
                for r in results:
                    if r.success and r.url:
                        collected_urls.append(r.url)
            elif results and results.url:
                collected_urls.append(results.url)

    except ImportError:
        log.warning("crawl4ai_not_installed")
        # Fallback: return known seed URLs
        collected_urls = [
            f"https://{base_domain}/help",
            f"https://{base_domain}/faq",
            f"https://{base_domain}/returns",
            f"https://{base_domain}/delivery",
        ]
    except Exception as e:
        log.warning("deep_crawl_failed", domain=base_domain, error=str(e))

    return list(dict.fromkeys(collected_urls))


# ══════════════════════════════════════════════════════════════════════════════
# EXTRACTION — crawl4ai LLM strategy (single URL) + ScrapeGraphAI (batch)
# ══════════════════════════════════════════════════════════════════════════════

def _llm_config_for_provider() -> dict:
    """Build provider/model/token config for both crawl4ai and ScrapeGraphAI."""
    if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        return {
            "provider": f"anthropic/{settings.primary_model}",
            "api_token": settings.anthropic_api_key,
            "model": settings.primary_model,
        }
    elif settings.llm_provider == "azure" and settings.azure_openai_api_key:
        return {
            "provider": f"azure/{settings.azure_primary_deployment}",
            "api_token": settings.azure_openai_api_key,
            "model": settings.azure_primary_deployment,
        }
    else:
        return {
            "provider": f"openai/{settings.openai_primary_model}",
            "api_token": settings.openai_api_key,
            "model": settings.openai_primary_model,
        }


async def _extract_batch_scrapegraph(
    urls: list[str], brand_name: str
) -> list[tuple[str, dict]]:
    """
    Use ScrapeGraphAI SmartScraperMultiGraph to extract structured data from
    multiple URLs in one LLM pass. Returns list of (url, extracted_dict).
    """
    if not urls:
        return []
    try:
        from scrapegraphai.graphs import SmartScraperMultiGraph

        cfg = _llm_config_for_provider()
        sg_config = {
            "llm": {
                "model": cfg["model"],
                "api_key": cfg["api_token"],
            },
            "headless": True,
            "verbose": False,
        }
        prompt = (
            f"For each page, extract structured information about {brand_name}: "
            "classify the page type (policy/faq/review/social/news/other), "
            "extract a summary, key facts, FAQ Q&A pairs if present, "
            "policy details (type, return window days, conditions), "
            "and review data (rating, platform, complaints, praises). "
            "Also determine the overall sentiment."
        )

        graph = SmartScraperMultiGraph(
            prompt=prompt,
            source=urls,
            config=sg_config,
        )
        result = graph.run()

        # SmartScraperMultiGraph returns a dict keyed by URL or a list
        if isinstance(result, dict):
            return [(url, result.get(url, {})) for url in urls if url in result]
        elif isinstance(result, list):
            return list(zip(urls, result))
        return []

    except ImportError:
        log.warning("scrapegraphai_not_installed", hint="pip install scrapegraphai")
        return []
    except Exception as e:
        log.warning("scrapegraph_batch_failed", error=str(e))
        return []


async def _extract_single_crawl4ai(url: str, brand_name: str) -> dict | None:
    """
    Fallback: crawl4ai LLM extraction strategy for a single URL.
    Used when ScrapeGraphAI is unavailable or for retries.
    """
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
        from crawl4ai.extraction_strategy import LLMExtractionStrategy, LLMConfig

        cfg = _llm_config_for_provider()
        strategy = LLMExtractionStrategy(
            llm_config=LLMConfig(
                provider=cfg["provider"],
                api_token=cfg["api_token"],
            ),
            schema=BRAND_PAGE_SCHEMA,
            instruction=(
                f"Extract structured support-relevant information about {brand_name}. "
                "Focus on: customer policies (returns, shipping, refunds), FAQs, "
                "reviews/complaints/praises, and news. "
                "Return exactly the JSON schema provided."
            ),
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
                return {"page_type": "other", "summary": extracted[:400], "key_facts": []}
        if isinstance(extracted, list):
            extracted = extracted[0] if extracted else None
        return extracted

    except ImportError:
        log.warning("crawl4ai_not_installed")
        return None
    except Exception as e:
        log.warning("crawl4ai_single_failed", url=url, error=str(e))
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STORAGE — FalkorDB graph + Postgres pgvector
# ══════════════════════════════════════════════════════════════════════════════

async def _write_to_graph(brand_id: str, url: str, extracted: dict) -> None:
    try:
        from core.ontology.schema import get_graph_client
        _, graph = get_graph_client()

        page_type = extracted.get("page_type", "other")
        summary = (extracted.get("summary") or "")[:500].replace("'", "")
        source_hash = url_hash(url)

        if page_type == "faq":
            for pair in extracted.get("faq_pairs") or []:
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
        import psycopg2
        parts = [extracted.get("summary") or ""]
        parts += [f"Q: {p['question']}\nA: {p['answer']}" for p in (extracted.get("faq_pairs") or [])]
        parts += [str(f) for f in (extracted.get("key_facts") or [])]
        content = "\n".join(parts).strip()
        if len(content) < 30:
            return

        conn = psycopg2.connect(
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
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

async def collect_brand_data(
    brand_id: str,
    brand_name: str,
    base_domain: str,
    resume: bool = True,
    max_urls: int = 200,
    concurrency: int = 5,
    scrapegraph_batch_size: int = 10,
) -> dict[str, Any]:
    """
    Full brand data collection pipeline using crawl4ai + ScrapeGraphAI.

    - Discovery: crawl4ai BFS on brand domain + DuckDuckGo HTML scraping
    - Extraction: ScrapeGraphAI multi-URL batch; crawl4ai single-URL fallback
    - Storage: FalkorDB graph + Postgres pgvector chunks
    - Dedup: Redis URL hash set + content hash map
    - Checkpoint: Redis JSON state saved after every batch
    """
    from core.memory.session import get_redis
    r = get_redis()
    dedup = DedupTracker(brand_id, r)
    ckpt = CheckpointManager(brand_id, r)

    state: CollectState | None = None
    if resume:
        state = await ckpt.load(brand_id)
        if state:
            log.info("collection_resumed", brand_id=brand_id, phase=state.phase, pending=len(state.pending_urls))

    if not state:
        state = CollectState(brand_id=brand_id)
        log.info("collection_started", brand_id=brand_id, domain=base_domain)

    t_start = time.monotonic()

    # ── Phase SEED: crawl4ai BFS on the brand's own domain ──────────────────
    if state.phase == CollectPhase.SEED:
        log.info("phase_seed", brand_id=brand_id)
        seed_urls = await _deep_crawl_domain(base_domain, max_pages=40)
        # Always include known support paths in case BFS misses them
        fallback_seeds = [
            f"https://{base_domain}/help",
            f"https://{base_domain}/faq",
            f"https://{base_domain}/returns",
            f"https://{base_domain}/delivery",
            f"https://{base_domain}/contact",
        ]
        for u in seed_urls + fallback_seeds:
            if u not in state.pending_urls:
                state.pending_urls.append(u)
        state.phase = CollectPhase.DISCOVER
        await ckpt.save(state)

    # ── Phase DISCOVER: DuckDuckGo HTML → external URLs ──────────────────────
    if state.phase == CollectPhase.DISCOVER:
        log.info("phase_discover", brand_id=brand_id)
        queries = _dork_queries(brand_name, base_domain)
        discovered = await _discover_via_duckduckgo(queries, max_per_query=6)
        for u in discovered:
            if not await dedup.is_url_seen(u) and u not in state.pending_urls:
                state.pending_urls.append(u)
        state.phase = CollectPhase.FETCH
        await ckpt.save(state)
        log.info("discovery_done", brand_id=brand_id, new_urls=len(discovered))

    # ── Phase FETCH: extract + store ─────────────────────────────────────────
    if state.phase == CollectPhase.FETCH:
        log.info("phase_fetch", brand_id=brand_id, queue=len(state.pending_urls))
        queue = [u for u in state.pending_urls if not await dedup.is_url_seen(u)]
        state.pending_urls = []
        processed = 0

        while queue and processed < max_urls:
            # Mark batch as visited immediately to avoid re-queuing
            batch = queue[:scrapegraph_batch_size]
            queue = queue[scrapegraph_batch_size:]
            for u in batch:
                await dedup.mark_url_seen(u)

            # Try ScrapeGraphAI batch first
            sg_results = await _extract_batch_scrapegraph(batch, brand_name)
            sg_done = {url for url, _ in sg_results}

            # crawl4ai fallback for any URL ScrapeGraphAI didn't handle
            sem = asyncio.Semaphore(concurrency)
            async def _fallback(url: str) -> tuple[str, dict | None]:
                async with sem:
                    extracted = await _extract_single_crawl4ai(url, brand_name)
                return url, extracted

            fallback_needed = [u for u in batch if u not in sg_done]
            fallback_results = await asyncio.gather(*[_fallback(u) for u in fallback_needed])

            all_results: list[tuple[str, dict | None]] = sg_results + list(fallback_results)

            for url, extracted in all_results:
                if not extracted:
                    state.error_count += 1
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

            state.visited_count += len(batch)
            processed += len(batch)
            await ckpt.save(state)  # checkpoint every batch

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
        "can_resume": state.phase != CollectPhase.DONE,
    }


async def get_collection_status(brand_id: str) -> dict | None:
    from core.memory.session import get_redis
    r = get_redis()
    ckpt = CheckpointManager(brand_id, r)
    state = await ckpt.load(brand_id)
    if not state:
        return None
    dedup = DedupTracker(brand_id, r)
    visited = await dedup.visited_count()
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
