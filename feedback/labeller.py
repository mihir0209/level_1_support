import structlog
from typing import Any
from core.orchestration.llm import get_llm_client
from brands.registry import get_brand_by_id
from domains.loader import get_domain_config

log = structlog.get_logger(__name__)

def _get_db_conn():
    from core.db.pool import get_sync_pool, ConnectionProxy
    return ConnectionProxy(get_sync_pool())

async def run_intent_labeller() -> int:
    """
    Async batch job that queries all unreviewed intent label queue items,
    classifies them using the LLM based on their domain config,
    and updates their suggested_label in PostgreSQL.

    Returns the number of processed queue items.
    """
    conn = None
    records = []
    try:
        conn = _get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, brand_id, raw_message
                    FROM intent_label_queue
                    WHERE reviewed = false
                """)
                records = cur.fetchall()
    except Exception as e:
        log.error("labeller_fetch_failed", error=str(e))
        return 0
    finally:
        if conn:
            conn.close()

    if not records:
        log.info("labeller_no_records_to_process")
        return 0

    log.info("labeller_processing_started", count=len(records))
    llm = get_llm_client()
    lc_client = llm.get_lc_client(fast=True)
    processed_count = 0

    for rec in records:
        queue_id, brand_id, raw_message = rec
        try:
            # Resolve domain config
            brand_entry = get_brand_by_id(brand_id)
            domain = brand_entry.domain if brand_entry else "ecommerce"
            cfg = get_domain_config(domain)

            # Build list of valid issue types
            options = []
            for it_id, it_spec in cfg.issue_types.items():
                options.append(f"- {it_id}: {it_spec.description}")
            options_str = "\n".join(options)

            # LLM Prompt
            prompt = f"""You are an expert intent classification assistant.
Your task is to re-classify a customer support query that was flagged as low confidence.

List of valid support intents for this domain:
{options_str}

Customer query:
"{raw_message}"

Respond with ONLY the exact intent slug (from the list above, e.g. "wismo", "return_initiate") that best fits the query. Do not include formatting, punctuation, or markdown. If none fit, respond with 'unknown'."""

            # Run LLM classification
            res = await lc_client.ainvoke(prompt)
            suggested = res.content.strip().replace('"', '').replace("'", "").lower()

            # Ensure it is a valid issue type
            if suggested not in cfg.issue_types and suggested != "unknown":
                log.warning("labeller_invalid_suggestion", queue_id=str(queue_id), suggested=suggested)
                suggested = "unknown"

            # Update PostgreSQL database
            conn = _get_db_conn()
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE intent_label_queue
                        SET suggested_label = %s
                        WHERE id = %s
                    """, (suggested, queue_id))
            conn.close()
            processed_count += 1
            log.info("labeller_item_processed", queue_id=str(queue_id), raw_message=raw_message[:50], suggested=suggested)

        except Exception as exc:
            log.error("labeller_item_failed", queue_id=str(queue_id), error=str(exc))

    log.info("labeller_processing_finished", processed=processed_count)
    return processed_count
