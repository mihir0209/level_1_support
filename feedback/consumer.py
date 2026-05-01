"""
Feedback Consumer — async worker that processes the review queues.

Runs as a background loop, polling:
  - intent_label_queue: aggregates low-confidence intent patterns
  - resolution_review_queue: surfaces failure patterns for operator review

The consumer is a lightweight daemon — it does not auto-correct, it surfaces
data for human review. It logs aggregated stats so operators can see systemic
resolution failures without needing to query the DB directly.

Triggered from main.py lifespan on startup, stopped on shutdown.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from config import settings

log = structlog.get_logger(__name__)

# How often the consumer wakes up (seconds)
_POLL_INTERVAL_SECONDS = 300  # 5 minutes


def _get_db_conn():
    """Open a Postgres connection using settings.postgres_conn_params()."""
    import psycopg
    return psycopg.connect(**settings.postgres_conn_params())


def _aggregate_intent_patterns() -> list[dict[str, Any]]:
    """Fetch top intent patterns needing review across all brands."""
    conn = None
    try:
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT brand_id, suggested_label, COUNT(*) AS cnt
                    FROM intent_label_queue
                    WHERE reviewed = false
                    GROUP BY brand_id, suggested_label
                    ORDER BY cnt DESC
                    LIMIT 10
                """)
                return [
                    {"brand_id": r[0], "intent": r[1], "count": r[2]}
                    for r in cur.fetchall()
                ]
    except Exception as e:
        log.warning("intent_aggregation_failed", error=str(e))
        return []
    finally:
        if conn:
            conn.close()


def _aggregate_resolution_failures() -> list[dict[str, Any]]:
    """Fetch top resolution failure patterns across all brands."""
    conn = None
    try:
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT brand_id, issue_type, failure_reason, COUNT(*) AS cnt
                    FROM resolution_review_queue
                    WHERE reviewed = false
                    GROUP BY brand_id, issue_type, failure_reason
                    ORDER BY cnt DESC
                    LIMIT 10
                """)
                return [
                    {
                        "brand_id": r[0],
                        "issue_type": r[1],
                        "failure_reason": (r[2] or "")[:120],
                        "count": r[3],
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        log.warning("failure_aggregation_failed", error=str(e))
        return []
    finally:
        if conn:
            conn.close()


def get_pending_counts() -> dict[str, int]:
    """Return counts of un-reviewed items in each queue."""
    conn = None
    try:
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM intent_label_queue WHERE reviewed = false")
                intent_row = cur.fetchone()
                intent_count = intent_row[0] if intent_row else 0

                cur.execute("SELECT COUNT(*) FROM resolution_review_queue WHERE reviewed = false")
                res_row = cur.fetchone()
                res_count = res_row[0] if res_row else 0

                return {"intent_labels_pending": intent_count, "resolution_reviews_pending": res_count}
    except Exception as e:
        log.warning("pending_counts_failed", error=str(e))
        return {"intent_labels_pending": 0, "resolution_reviews_pending": 0}
    finally:
        if conn:
            conn.close()


def sync_aggregate() -> dict[str, Any]:
    """
    Synchronous one-shot: aggregate stats and return.
    Used by admin dashboard to get current state without starting the consumer loop.
    """
    intent_patterns = _aggregate_intent_patterns()
    failure_patterns = _aggregate_resolution_failures()
    counts = get_pending_counts()

    log.info(
        "feedback_aggregate",
        intent_pending=counts["intent_labels_pending"],
        resolution_pending=counts["resolution_reviews_pending"],
        top_intents=len(intent_patterns),
        top_failures=len(failure_patterns),
    )

    return {
        **counts,
        "top_intent_patterns": intent_patterns,
        "top_failure_patterns": failure_patterns,
    }


# ── Background consumer loop ────────────────────────────────────────────────────

_consumer_task: asyncio.Task | None = None
_running = False


async def _consumer_loop() -> None:
    """
    Background loop that wakes every POLL_INTERVAL_SECONDS and logs
    aggregated queue stats. Designed for observability — operators see
    systemic failure patterns in Grafana/logs without querying the DB.
    """
    global _running
    log.info("feedback_consumer_started", poll_interval=_POLL_INTERVAL_SECONDS)

    while _running:
        try:
            stats = sync_aggregate()
            log.info(
                "feedback_consumer_poll",
                intent_pending=stats["intent_labels_pending"],
                resolution_pending=stats["resolution_reviews_pending"],
                top_intents=stats["top_intent_patterns"][:3] if stats["top_intent_patterns"] else [],
                top_failures=stats["top_failure_patterns"][:3] if stats["top_failure_patterns"] else [],
            )
        except Exception as e:
            log.warning("feedback_consumer_poll_failed", error=str(e))

        # Sleep in small increments so we can stop cleanly
        for _ in range(_POLL_INTERVAL_SECONDS):
            if not _running:
                break
            await asyncio.sleep(1)

    log.info("feedback_consumer_stopped")


def start_consumer() -> None:
    """Start the background consumer loop from the main asyncio event loop."""
    global _consumer_task, _running
    if _consumer_task and not _consumer_task.done():
        log.warning("feedback_consumer_already_running")
        return
    _running = True
    loop = asyncio.new_event_loop()
    _consumer_task = loop.create_task(_consumer_loop())
    # Attach the loop to a thread-pool thread so it runs without blocking main thread
    import threading
    t = threading.Thread(target=lambda: asyncio.run(_consumer_loop()), daemon=True)
    t.start()
    log.info("feedback_consumer_thread_started")


def stop_consumer() -> None:
    """Signal the consumer to stop."""
    global _running
    _running = False
    if _consumer_task:
        _consumer_task.cancel()
    log.info("feedback_consumer_signalled_stop")