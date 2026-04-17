"""
APScheduler-based ingestion scheduler.

Runs crawl-to-graph pipelines on a configurable schedule per brand.
Default: weekly full crawl (Sundays 02:00 UTC), daily delta check (03:00 UTC).

Integrates with the FastAPI lifespan so it starts/stops with the app.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from brands.registry import BRAND_REGISTRY
from ingestion.pipeline import run_pipeline

log = structlog.get_logger(__name__)

# ── Scheduler singleton ───────────────────────────────────────────────────────

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


# ── Job functions ─────────────────────────────────────────────────────────────

async def _full_crawl_job(brand_id: str, base_url: str) -> None:
    """Weekly full crawl job for a brand."""
    log.info("scheduled_crawl_start", brand_id=brand_id, job="full_crawl")
    try:
        report = await run_pipeline(brand_id=brand_id, base_url=base_url, max_pages=100)
        log.info(
            "scheduled_crawl_done",
            brand_id=brand_id,
            pages=report.pages_crawled,
            graph_upserted=report.graph_upserted,
        )
    except Exception as exc:
        log.error("scheduled_crawl_failed", brand_id=brand_id, error=str(exc))


async def _delta_check_job(brand_id: str, base_url: str) -> None:
    """Daily lighter crawl — checks frequently-changing pages (returns, promos)."""
    log.info("scheduled_delta_start", brand_id=brand_id, job="delta_check")
    try:
        # Only crawl a smaller subset for daily delta
        report = await run_pipeline(brand_id=brand_id, base_url=base_url, max_pages=15)
        log.info(
            "scheduled_delta_done",
            brand_id=brand_id,
            pages=report.pages_crawled,
            graph_upserted=report.graph_upserted,
        )
    except Exception as exc:
        log.error("scheduled_delta_failed", brand_id=brand_id, error=str(exc))


# ── Schedule registration ─────────────────────────────────────────────────────

def register_brand_jobs(scheduler: AsyncIOScheduler) -> None:
    """Register weekly + daily jobs for every brand in the registry."""
    for brand_id, entry in BRAND_REGISTRY.items():
        base_url = f"https://{entry.domain}"

        # Weekly full crawl — Sundays 02:00 UTC, staggered by 10 mins per brand
        brands_list = list(BRAND_REGISTRY.keys())
        offset_minutes = brands_list.index(brand_id) * 10
        full_hour = 2
        full_minute = offset_minutes % 60
        full_hour += offset_minutes // 60

        scheduler.add_job(
            _full_crawl_job,
            trigger=CronTrigger(day_of_week="sun", hour=full_hour, minute=full_minute),
            args=[brand_id, base_url],
            id=f"full_crawl_{brand_id}",
            name=f"Full crawl: {brand_id}",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        # Daily delta — 03:00 UTC + stagger
        delta_minute = (offset_minutes + 3) % 60
        scheduler.add_job(
            _delta_check_job,
            trigger=CronTrigger(hour=3, minute=delta_minute),
            args=[brand_id, base_url],
            id=f"delta_check_{brand_id}",
            name=f"Delta check: {brand_id}",
            replace_existing=True,
            misfire_grace_time=1800,
        )

    log.info("scheduler_jobs_registered", count=len(BRAND_REGISTRY) * 2)


def start_scheduler() -> AsyncIOScheduler:
    """Start the scheduler and register all brand jobs. Called from FastAPI lifespan."""
    scheduler = get_scheduler()
    register_brand_jobs(scheduler)
    scheduler.start()
    log.info("scheduler_started", jobs=len(scheduler.get_jobs()))
    return scheduler


def stop_scheduler() -> None:
    """Gracefully stop the scheduler. Called from FastAPI lifespan shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")


# ── Admin helpers (exposed via API) ──────────────────────────────────────────

async def trigger_crawl_now(brand_id: str) -> dict[str, Any]:
    """Manually trigger a full crawl for a brand (called from admin API endpoint)."""
    entry = BRAND_REGISTRY.get(brand_id)
    if not entry:
        return {"error": f"unknown brand: {brand_id}"}

    base_url = f"https://{entry.domain}"
    log.info("manual_crawl_triggered", brand_id=brand_id)

    # Run in background task — return immediately
    asyncio.create_task(_full_crawl_job(brand_id, base_url))
    return {
        "status": "triggered",
        "brand_id": brand_id,
        "base_url": base_url,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
    }


def list_jobs() -> list[dict[str, Any]]:
    """List all scheduled jobs with next run times."""
    scheduler = get_scheduler()
    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.isoformat() if next_run else None,
        })
    return jobs
