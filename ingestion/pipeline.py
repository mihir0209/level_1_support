"""
Full crawl-to-graph ingestion pipeline.

Stages:
  1. Crawl — Firecrawl discovers + scrapes brand support pages
  2. Extract — LLM extracts structured facts from each page
  3. Validate — confidence thresholds route to auto-approve / human review / reject
  4. Upsert — auto-approved facts written to FalkorDB knowledge graph
  5. Queue — human-review items written to Postgres review queue
  6. Embed — FAQ chunks embedded and stored in Postgres pgvector table

Entry point: run_pipeline(brand_id, base_url) → PipelineReport
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from ingestion.crawler import crawl_brand, CrawledPage
from ingestion.extractor import extract_facts, ExtractedFacts
from ingestion.validator import validate_facts, ValidationReport, ValidationOutcome, ValidationResult

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Graph upsert helpers
# ═══════════════════════════════════════════════════════════════════════════

async def _upsert_to_graph(result: ValidationResult) -> bool:
    """Write a validated fact to the FalkorDB knowledge graph."""
    from core.ontology.schema import get_graph_client

    try:
        _, graph = get_graph_client()

        if result.item_type == "policy":
            d = result.item_data
            # Build a unique policy_id from brand + type + content hash
            policy_id = f"{result.brand_id}_{d['type']}_{result.content_hash}"
            conditions_json = str(d.get("conditions", []))
            regions_str = ",".join(d.get("regions", ["all"]))
            valid_from = d.get("valid_from") or "2020-01-01"
            valid_until = d.get("valid_until") or "2099-12-31"

            query = (
                f"MERGE (p:Policy {{policy_id: '{policy_id}'}}) "
                f"SET p.brand_id = '{result.brand_id}', "
                f"    p.type = '{d['type']}', "
                f"    p.summary = '{d['summary'].replace(chr(39), '')}', "
                f"    p.conditions = '{conditions_json.replace(chr(39), '')}', "
                f"    p.valid_from = '{valid_from}', "
                f"    p.valid_until = '{valid_until}', "
                f"    p.regions = '{regions_str}', "
                f"    p.confidence = {result.confidence}, "
                f"    p.source_url = '{result.source_url}', "
                f"    p.updated_at = '{datetime.now(timezone.utc).isoformat()}' "
                f"WITH p "
                f"MATCH (b:Brand {{brand_id: '{result.brand_id}'}}) "
                f"MERGE (b)-[:HAS_POLICY]->(p)"
            )
            graph.query(query)

        elif result.item_type == "faq":
            d = result.item_data
            faq_id = f"{result.brand_id}_faq_{result.content_hash}"
            tags_str = ",".join(d.get("intent_tags", []))
            question_safe = d["question"].replace("'", "")
            answer_safe = d["answer"].replace("'", "")[:500]

            query = (
                f"MERGE (f:FAQ {{faq_id: '{faq_id}'}}) "
                f"SET f.brand_id = '{result.brand_id}', "
                f"    f.question = '{question_safe}', "
                f"    f.answer = '{answer_safe}', "
                f"    f.intent_tags = '{tags_str}', "
                f"    f.confidence = {result.confidence}, "
                f"    f.source_url = '{result.source_url}', "
                f"    f.updated_at = '{datetime.now(timezone.utc).isoformat()}' "
                f"WITH f "
                f"MATCH (b:Brand {{brand_id: '{result.brand_id}'}}) "
                f"MERGE (b)-[:HAS_FAQ]->(f)"
            )
            graph.query(query)

        elif result.item_type == "brand_fact":
            d = result.item_data
            fact_id = f"{result.brand_id}_fact_{result.content_hash}"
            content_safe = d["content"].replace("'", "")[:300]

            query = (
                f"MERGE (bf:BrandFact {{fact_id: '{fact_id}'}}) "
                f"SET bf.brand_id = '{result.brand_id}', "
                f"    bf.fact_type = '{d['fact_type']}', "
                f"    bf.content = '{content_safe}', "
                f"    bf.confidence = {result.confidence}, "
                f"    bf.source_url = '{result.source_url}', "
                f"    bf.updated_at = '{datetime.now(timezone.utc).isoformat()}' "
                f"WITH bf "
                f"MATCH (b:Brand {{brand_id: '{result.brand_id}'}}) "
                f"MERGE (b)-[:HAS_FACT]->(bf)"
            )
            graph.query(query)

        return True
    except Exception as exc:
        log.error("graph_upsert_failed", item_type=result.item_type, error=str(exc))
        return False


async def _queue_for_review(results: list[ValidationResult]) -> int:
    """Write human-review items to Postgres resolution_review_queue."""
    if not results:
        return 0
    try:
        import asyncpg
        from config import settings

        conn = await asyncpg.connect(settings.postgres_dsn.replace("+asyncpg", ""))
        inserted = 0
        for r in results:
            import json as _json
            await conn.execute(
                """
                INSERT INTO resolution_review_queue
                    (brand_id, item_type, item_data, confidence, source_url, status, created_at)
                VALUES ($1, $2, $3, $4, $5, 'pending', NOW())
                ON CONFLICT DO NOTHING
                """,
                r.brand_id,
                r.item_type,
                _json.dumps(r.item_data),
                r.confidence,
                r.source_url,
            )
            inserted += 1
        await conn.close()
        log.info("review_queue_inserted", count=inserted)
        return inserted
    except Exception as exc:
        log.error("review_queue_failed", error=str(exc))
        return 0


async def _embed_and_store_faqs(results: list[ValidationResult]) -> int:
    """Embed FAQ content and store in Postgres pgvector for vector fallback search."""
    faq_results = [r for r in results if r.item_type == "faq"]
    if not faq_results:
        return 0
    try:
        from core.orchestration.llm import get_llm_client
        import asyncpg
        from config import settings

        llm = get_llm_client()
        conn = await asyncpg.connect(settings.postgres_dsn.replace("+asyncpg", ""))
        stored = 0
        for r in faq_results:
            d = r.item_data
            text = f"Q: {d['question']}\nA: {d['answer']}"
            embedding = await llm.embed(text)
            chunk_id = f"{r.brand_id}_faq_{r.content_hash}"
            await conn.execute(
                """
                INSERT INTO knowledge_chunks
                    (chunk_id, brand_id, source_url, chunk_type, content, embedding, confidence, created_at)
                VALUES ($1, $2, $3, 'faq', $4, $5, $6, NOW())
                ON CONFLICT (chunk_id) DO UPDATE
                    SET content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding,
                        confidence = EXCLUDED.confidence
                """,
                chunk_id,
                r.brand_id,
                r.source_url,
                text,
                embedding,
                r.confidence,
            )
            stored += 1
        await conn.close()
        log.info("faqs_embedded_stored", count=stored)
        return stored
    except Exception as exc:
        log.error("faq_embed_failed", error=str(exc))
        return 0


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline report
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PipelineReport:
    brand_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    pages_crawled: int = 0
    facts_extracted: int = 0
    auto_approved: int = 0
    human_review_queued: int = 0
    rejected: int = 0
    graph_upserted: int = 0
    faqs_embedded: int = 0
    errors: list[str] = field(default_factory=list)

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "pages_crawled": self.pages_crawled,
            "facts_extracted": self.facts_extracted,
            "auto_approved": self.auto_approved,
            "human_review_queued": self.human_review_queued,
            "rejected": self.rejected,
            "graph_upserted": self.graph_upserted,
            "faqs_embedded": self.faqs_embedded,
            "errors": self.errors,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Main pipeline entry point
# ═══════════════════════════════════════════════════════════════════════════

async def run_pipeline(
    brand_id: str,
    base_url: str,
    max_pages: int = 50,
    concurrency: int = 3,
) -> PipelineReport:
    """
    Run the full crawl → extract → validate → upsert pipeline for one brand.

    Returns PipelineReport with counts for monitoring/alerting.
    """
    report = PipelineReport(brand_id=brand_id)
    log.info("pipeline_start", brand_id=brand_id, base_url=base_url)

    all_validation_reports: list[ValidationReport] = []

    # Stage 1+2: Crawl and extract in a streaming fashion
    async for page in crawl_brand(brand_id, base_url, max_pages=max_pages, concurrency=concurrency):
        report.pages_crawled += 1
        try:
            facts = await extract_facts(page)
            report.facts_extracted += facts.total_items

            # Stage 3: Validate
            validation = validate_facts(facts)
            all_validation_reports.append(validation)
            report.auto_approved += len(validation.auto_approve)
            report.human_review_queued += len(validation.human_review)
            report.rejected += len(validation.rejected)
        except Exception as exc:
            msg = f"page_failed url={page.url} error={exc}"
            log.error("pipeline_page_error", url=page.url, error=str(exc))
            report.errors.append(msg)

    # Stage 4: Upsert all auto-approved items to graph
    all_auto_approved = [r for vr in all_validation_reports for r in vr.auto_approve]
    upsert_tasks = [_upsert_to_graph(r) for r in all_auto_approved]
    upsert_results = await asyncio.gather(*upsert_tasks, return_exceptions=True)
    report.graph_upserted = sum(1 for r in upsert_results if r is True)

    # Stage 5: Queue human review items to Postgres
    all_review = [r for vr in all_validation_reports for r in vr.human_review]
    await _queue_for_review(all_review)

    # Stage 6: Embed FAQs (auto-approved + human review for search)
    all_faqs = all_auto_approved + all_review
    report.faqs_embedded = await _embed_and_store_faqs(all_faqs)

    report.finish()
    log.info("pipeline_done", brand_id=brand_id, **{k: v for k, v in report.to_dict().items() if k not in ("brand_id", "started_at", "finished_at", "errors")})
    return report


async def run_url(brand_id: str, url: str) -> PipelineReport:
    """Single-URL ingestion (on-demand, e.g. when a policy page changes)."""
    from ingestion.crawler import crawl_url

    report = PipelineReport(brand_id=brand_id)
    page = await crawl_url(brand_id, url)
    if not page:
        report.errors.append(f"failed to crawl {url}")
        report.finish()
        return report

    report.pages_crawled = 1
    facts = await extract_facts(page)
    report.facts_extracted = facts.total_items

    validation = validate_facts(facts)
    report.auto_approved = len(validation.auto_approve)
    report.human_review_queued = len(validation.human_review)
    report.rejected = len(validation.rejected)

    upsert_results = await asyncio.gather(
        *[_upsert_to_graph(r) for r in validation.auto_approve],
        return_exceptions=True,
    )
    report.graph_upserted = sum(1 for r in upsert_results if r is True)

    await _queue_for_review(validation.human_review)
    report.faqs_embedded = await _embed_and_store_faqs(validation.auto_approve + validation.human_review)

    report.finish()
    return report
