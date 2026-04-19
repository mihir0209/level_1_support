"""
Brand data collection orchestrator.

Flow for a newly registered brand:
  1. Seed — crawl the brand's own website (support pages, policies, FAQs)
  2. Discover — web-search for news, reviews, social mentions about the brand
  3. Fetch — crawl each discovered URL for structured content
  4. Extract — pull typed facts (policy, FAQ, review, news, social) using LLM
  5. Deduplicate — skip any URL/content already seen (Redis hash set)
  6. Store — write to FalkorDB graph + Postgres pgvector
  7. Checkpoint — persist crawl state to Redis so runs can resume

Deduplication:
  Redis SET  brand:collect:{brand_id}:visited_urls  → SHA256(canonicalized URL)
  Redis HASH brand:collect:{brand_id}:content_hashes → SHA256(content) → URL

Checkpoint state (Redis JSON):
  brand:collect:{brand_id}:state → {queue, visited_count, phase, started_at, updated_at}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator
from urllib.parse import urlparse, urlunparse

import structlog

from config import settings

log = structlog.get_logger(__name__)

# ── Redis key helpers ─────────────────────────────────────────────────────────
def _rkey(brand_id: str, suffix: str) -> str:
    return f"brand:collect:{brand_id}:{suffix}"


# ── URL canonicalization + hashing ────────────────────────────────────────────
def canonical_url(url: str) -> str:
    """Strip tracking params, normalize scheme/host, drop fragments."""
    try:
        p = urlparse(url.strip())
        # Rebuild without query params that are tracking-only
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


# ── Extraction schemas (Pydantic-compatible dicts for crawl4ai / ScrapeGraph) ──
BRAND_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {
            "type": "string",
            "enum": ["policy", "faq", "about", "product", "news", "review", "social", "other"],
            "description": "What kind of page this is",
        },
        "brand_name": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string", "description": "2-3 sentence summary of the page content"},
        "key_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Important facts, policies, or claims on this page",
        },
        "sentiment": {
            "type": "string",
            "enum": ["positive", "neutral", "negative", "mixed"],
            "description": "Overall tone/sentiment of the content",
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                },
            },
            "description": "Named entities: products, people, places, policies",
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

# ── Search query templates ────────────────────────────────────────────────────
def _build_search_queries(brand_name: str, domain: str) -> list[str]:
    """Generate multi-source discovery queries for a brand."""
    return [
        f'"{brand_name}" customer reviews complaints',
        f'"{brand_name}" return policy shipping delivery 2024 2025',
        f'site:trustpilot.com "{brand_name}"',
        f'site:reddit.com "{brand_name}" customer service',
        f'"{brand_name}" news 2024 2025',
        f'"{brand_name}" social media controversy OR complaint OR praise',
        f'"{brand_name}" review site:reviews.io OR site:resellerratings.com',
    ]


# ══════════════════════════════════════════════════════════════════════════════
# DEDUP TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class DedupTracker:
    """Redis-backed deduplication for URLs and content."""

    def __init__(self, brand_id: str, redis_client):
        self._r = redis_client
        self._url_key = _rkey(brand_id, "visited_urls")
        self._content_key = _rkey(brand_id, "content_hashes")

    async def is_url_seen(self, url: str) -> bool:
        h = url_hash(url)
        return bool(await self._r.sismember(self._url_key, h))

    async def mark_url_seen(self, url: str) -> None:
        h = url_hash(url)
        await self._r.sadd(self._url_key, h)

    async def is_content_duplicate(self, text: str) -> bool:
        h = content_hash(text)
        return bool(await self._r.hexists(self._content_key, h))

    async def mark_content_seen(self, text: str, url: str) -> None:
        h = content_hash(text)
        await self._r.hset(self._content_key, h, url)

    async def visited_count(self) -> int:
        return await self._r.scard(self._url_key)


# ══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class CheckpointManager:
    """Persist and restore crawl state in Redis."""

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

    async def clear(self, brand_id: str) -> None:
        await self._r.delete(self._key)


# ══════════════════════════════════════════════════════════════════════════════
# WEB SEARCH (Exa API — no scraping restrictions)
# ══════════════════════════════════════════════════════════════════════════════

async def _search_urls(queries: list[str], max_per_query: int = 5) -> list[str]:
    """
    Discover URLs via Exa (neural web search — no Google TOS restrictions).
    Falls back to a simple HTTP search if Exa key not configured.
    """
    urls: list[str] = []

    exa_key = getattr(settings, "exa_api_key", "") or ""
    if exa_key:
        try:
            from exa_py import Exa
            exa = Exa(api_key=exa_key)
            for q in queries:
                result = exa.search(q, num_results=max_per_query, use_autoprompt=True)
                for r in result.results:
                    if r.url:
                        urls.append(r.url)
        except Exception as e:
            log.warning("exa_search_failed", error=str(e))
    else:
        # Fallback: use Bing/DuckDuckGo via httpx if Exa not configured
        log.info("exa_key_not_set", hint="Set EXA_API_KEY for better search coverage")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                for q in queries[:3]:  # limit fallback queries
                    resp = await client.get(
                        "https://api.duckduckgo.com/",
                        params={"q": q, "format": "json", "no_html": "1"},
                        headers={"User-Agent": "L1SupportBot/1.0"},
                    )
                    data = resp.json()
                    for item in data.get("RelatedTopics", [])[:max_per_query]:
                        if isinstance(item, dict) and item.get("FirstURL"):
                            urls.append(item["FirstURL"])
        except Exception as e:
            log.warning("duckduckgo_fallback_failed", error=str(e))

    return list(dict.fromkeys(urls))  # dedupe preserving order


# ══════════════════════════════════════════════════════════════════════════════
# CRAWL + EXTRACT (crawl4ai)
# ══════════════════════════════════════════════════════════════════════════════

async def _crawl_and_extract(url: str, brand_id: str, brand_name: str) -> dict | None:
    """
    Crawl a URL with crawl4ai and extract structured data using an LLM.
    Returns the extracted dict or None on failure.
    """
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
        from crawl4ai.extraction_strategy import LLMExtractionStrategy, LLMConfig

        llm_provider = settings.llm_provider
        if llm_provider == "anthropic":
            provider_str = f"anthropic/{settings.primary_model}"
            api_token = settings.anthropic_api_key
        elif llm_provider == "azure":
            provider_str = f"azure/{settings.azure_primary_deployment}"
            api_token = settings.azure_openai_api_key
        else:
            provider_str = f"openai/{settings.openai_primary_model}"
            api_token = settings.openai_api_key

        strategy = LLMExtractionStrategy(
            llm_config=LLMConfig(provider=provider_str, api_token=api_token),
            schema=BRAND_PAGE_SCHEMA,
            instruction=(
                f"Extract structured information about {brand_name} from this page. "
                "Focus on customer support relevant content: policies, FAQs, reviews, news."
            ),
        )

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                url=url,
                config=CrawlerRunConfig(
                    extraction_strategy=strategy,
                    cache_mode=CacheMode.BYPASS,
                    word_count_threshold=50,
                    exclude_external_links=False,
                    wait_for="body",
                ),
            )

        if not result.success:
            return None

        extracted = result.extracted_content
        if isinstance(extracted, str):
            import json as _json
            try:
                extracted = _json.loads(extracted)
            except Exception:
                extracted = {"page_type": "other", "summary": extracted[:500], "key_facts": []}

        if isinstance(extracted, list):
            extracted = extracted[0] if extracted else None

        return extracted

    except ImportError:
        log.warning("crawl4ai_not_installed", url=url)
        return await _simple_fetch_extract(url, brand_name)
    except Exception as e:
        log.warning("crawl4ai_failed", url=url, error=str(e))
        return None


async def _simple_fetch_extract(url: str, brand_name: str) -> dict | None:
    """Fallback: plain httpx fetch + basic text extraction (no LLM)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "L1SupportBot/1.0"})
            text = resp.text[:4000]
        return {
            "page_type": "other",
            "summary": f"Raw content from {url} ({len(text)} chars)",
            "key_facts": [],
            "raw_text": text,
        }
    except Exception as e:
        log.warning("simple_fetch_failed", url=url, error=str(e))
        return None


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH + VECTOR WRITER
# ══════════════════════════════════════════════════════════════════════════════

async def _write_to_graph(brand_id: str, url: str, extracted: dict) -> None:
    """Write extracted data into FalkorDB based on page type."""
    try:
        from core.ontology.schema import get_graph_client
        _, graph = get_graph_client()

        page_type = extracted.get("page_type", "other")
        summary = (extracted.get("summary") or "")[:500].replace("'", "")
        source_hash = url_hash(url)

        if page_type == "faq":
            for pair in extracted.get("faq_pairs", []):
                q = (pair.get("question") or "")[:400].replace("'", "")
                a = (pair.get("answer") or "")[:800].replace("'", "")
                if not q:
                    continue
                faq_id = f"{brand_id}_{hashlib.sha256(q.encode()).hexdigest()[:12]}"
                graph.query(
                    f"MERGE (f:FAQ {{faq_id: '{faq_id}'}}) "
                    f"SET f.brand_id = '{brand_id}', f.question = '{q}', f.answer = '{a}', "
                    f"    f.source_url = '{url}', f.category = 'collected' "
                    f"WITH f MATCH (b:Brand {{brand_id: '{brand_id}'}}) "
                    f"MERGE (b)-[:HAS_FAQ]->(f)"
                )

        elif page_type == "policy":
            pd = extracted.get("policy_details") or {}
            policy_type = (pd.get("policy_type") or "general").replace("'", "")
            window = pd.get("window_days") or 0
            conditions = json.dumps(pd.get("conditions") or []).replace("'", "")
            policy_id = f"{brand_id}_{policy_type}_{source_hash[:8]}"
            graph.query(
                f"MERGE (p:Policy {{policy_id: '{policy_id}'}}) "
                f"SET p.brand_id = '{brand_id}', p.policy_type = '{policy_type}', "
                f"    p.title = '{policy_type.replace('_', ' ').title()}', "
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
            complaints_json = json.dumps(rd.get("common_complaints") or []).replace("'", "")
            praises_json = json.dumps(rd.get("common_praises") or []).replace("'", "")
            mention_id = f"{brand_id}_{page_type}_{source_hash[:10]}"
            graph.query(
                f"MERGE (m:BrandMention {{mention_id: '{mention_id}'}}) "
                f"SET m.brand_id = '{brand_id}', m.type = '{page_type}', "
                f"    m.sentiment = '{sentiment}', m.platform = '{platform}', "
                f"    m.rating = {rating}, m.summary = '{summary}', "
                f"    m.complaints = '{complaints_json}', m.praises = '{praises_json}', "
                f"    m.source_url = '{url}' "
                f"WITH m MATCH (b:Brand {{brand_id: '{brand_id}'}}) "
                f"MERGE (b)-[:HAS_MENTION]->(m)"
            )

        else:
            # General knowledge node — store summary as a KnowledgeItem
            item_id = f"{brand_id}_ki_{source_hash[:10]}"
            facts_json = json.dumps(extracted.get("key_facts") or []).replace("'", "")
            graph.query(
                f"MERGE (k:KnowledgeItem {{item_id: '{item_id}'}}) "
                f"SET k.brand_id = '{brand_id}', k.page_type = '{page_type}', "
                f"    k.summary = '{summary}', k.key_facts = '{facts_json}', "
                f"    k.source_url = '{url}' "
                f"WITH k MATCH (b:Brand {{brand_id: '{brand_id}'}}) "
                f"MERGE (b)-[:HAS_KNOWLEDGE]->(k)"
            )

    except Exception as e:
        log.warning("graph_write_failed", url=url, brand_id=brand_id, error=str(e))


async def _write_to_pgvector(brand_id: str, url: str, extracted: dict) -> None:
    """Write summary + key_facts as a searchable chunk to Postgres pgvector."""
    try:
        import psycopg2
        content = "\n".join([
            extracted.get("summary") or "",
            *([f"Q: {p['question']}\nA: {p['answer']}" for p in extracted.get("faq_pairs") or []]),
            *[str(f) for f in (extracted.get("key_facts") or [])],
        ]).strip()

        if len(content) < 30:
            return

        conn = psycopg2.connect(
            host=settings.postgres_host, port=settings.postgres_port,
            dbname=settings.postgres_db, user=settings.postgres_user,
            password=settings.postgres_password,
        )
        try:
            cur = conn.cursor()
            c_hash = content_hash(content)
            cur.execute(
                """
                INSERT INTO knowledge_chunks (brand_id, chunk_type, content, metadata)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (brand_id, extracted.get("page_type", "collected"),
                 content[:8000], json.dumps({"source_url": url, "content_hash": c_hash})),
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
) -> dict[str, Any]:
    """
    Full brand data collection pipeline.

    Args:
        brand_id:    canonical brand identifier
        brand_name:  human-readable brand name (used in search queries)
        base_domain: primary domain, e.g. "boohoo.com"
        resume:      if True, resume from saved checkpoint; False = fresh start
        max_urls:    hard limit on total URLs to fetch per run
        concurrency: parallel fetcher count

    Returns:
        summary dict with counts and final phase
    """
    from core.memory.session import get_redis
    r = get_redis()
    dedup = DedupTracker(brand_id, r)
    ckpt = CheckpointManager(brand_id, r)

    # ── Load or create state ────────────────────────────────────────────────
    state: CollectState | None = None
    if resume:
        state = await ckpt.load(brand_id)
        if state:
            log.info("collection_resumed", brand_id=brand_id, phase=state.phase, pending=len(state.pending_urls))

    if not state:
        state = CollectState(brand_id=brand_id)
        log.info("collection_started", brand_id=brand_id, domain=base_domain)

    t_start = time.monotonic()

    # ── Phase: SEED — crawl the brand's own domain ──────────────────────────
    if state.phase == CollectPhase.SEED:
        seed_urls = [
            f"https://{base_domain}/",
            f"https://{base_domain}/help",
            f"https://{base_domain}/faq",
            f"https://{base_domain}/returns",
            f"https://{base_domain}/delivery",
            f"https://{base_domain}/contact",
            f"https://{base_domain}/shipping",
            f"https://{base_domain}/sitemap.xml",
        ]
        for u in seed_urls:
            if not await dedup.is_url_seen(u):
                state.pending_urls.append(u)

        state.phase = CollectPhase.DISCOVER
        await ckpt.save(state)

    # ── Phase: DISCOVER — search web for news/reviews/social ───────────────
    if state.phase == CollectPhase.DISCOVER:
        queries = _build_search_queries(brand_name, base_domain)
        discovered = await _search_urls(queries, max_per_query=5)
        for u in discovered:
            if not await dedup.is_url_seen(u):
                state.pending_urls.append(u)
        state.phase = CollectPhase.FETCH
        await ckpt.save(state)
        log.info("discovery_done", brand_id=brand_id, discovered=len(discovered))

    # ── Phase: FETCH — process the URL queue ────────────────────────────────
    if state.phase == CollectPhase.FETCH:
        sem = asyncio.Semaphore(concurrency)

        async def _process_one(url: str) -> bool:
            if await dedup.is_url_seen(url):
                return False
            await dedup.mark_url_seen(url)

            async with sem:
                extracted = await _crawl_and_extract(url, brand_id, brand_name)

            if not extracted:
                state.error_count += 1
                return False

            raw_text = json.dumps(extracted)
            if await dedup.is_content_duplicate(raw_text):
                return False
            await dedup.mark_content_seen(raw_text, url)

            await asyncio.gather(
                _write_to_graph(brand_id, url, extracted),
                _write_to_pgvector(brand_id, url, extracted),
            )
            state.extracted_count += 1
            return True

        processed = 0
        queue = list(state.pending_urls)
        state.pending_urls = []

        while queue and processed < max_urls:
            batch = queue[:concurrency]
            queue = queue[concurrency:]
            results = await asyncio.gather(*[_process_one(u) for u in batch], return_exceptions=True)
            processed += sum(1 for r in results if r is True)
            state.visited_count += len(batch)
            await ckpt.save(state)  # checkpoint after each batch

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
    """Return current checkpoint state for a brand, or None if not started."""
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
