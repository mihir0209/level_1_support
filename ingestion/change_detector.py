import hashlib
import structlog
from datetime import datetime, timezone
import asyncpg
from config import settings

log = structlog.get_logger(__name__)

def calculate_hash(content: str) -> str:
    """Calculate the SHA-256 hash of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

async def detect_change_and_update(brand_id: str, url: str, content: str) -> bool:
    """
    Checks if the crawled content of a URL has changed by comparing SHA-256 hashes.
    Persists/updates the page hash in the crawl_sources table.

    Returns:
        True if the page is new or has changed, False otherwise.
    """
    current_hash = calculate_hash(content)
    now = datetime.now(timezone.utc)
    
    conn = None
    try:
        conn = await asyncpg.connect(settings.postgres_dsn.replace("+asyncpg", ""))
        
        # Check if URL exists in crawl_sources
        row = await conn.fetchrow(
            "SELECT last_hash FROM crawl_sources WHERE url = $1",
            url
        )
        
        if row is None:
            # New page source
            await conn.execute(
                """
                INSERT INTO crawl_sources (brand_id, url, source_type, last_crawled_at, last_hash)
                VALUES ($1, $2, 'help_center', $3, $4)
                """,
                brand_id, url, now, current_hash
            )
            log.info("change_detector_new_source", brand_id=brand_id, url=url)
            return True
            
        last_hash = row["last_hash"]
        if last_hash == current_hash:
            # Content hasn't changed
            await conn.execute(
                "UPDATE crawl_sources SET last_crawled_at = $1 WHERE url = $2",
                now, url
            )
            log.debug("change_detector_no_change", url=url)
            return False
            
        # Content has changed
        await conn.execute(
            """
            UPDATE crawl_sources
            SET last_hash = $1, last_crawled_at = $2
            WHERE url = $3
            """,
            current_hash, now, url
        )
        log.info("change_detector_change_detected", brand_id=brand_id, url=url)

        # Trigger n8n policy change webhook (in background executor)
        try:
            from core.utils.http import http_request
            n8n_policy_url = f"{settings.n8n_url.rstrip('/')}/webhook/policy-change"
            import asyncio
            
            n8n_payload = {
                "brand_id": brand_id,
                "url": url,
                "new_hash": current_hash,
                "timestamp": now.isoformat()
            }
            
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                None,
                lambda: http_request(
                    "POST",
                    n8n_policy_url,
                    json_body=n8n_payload,
                    timeout=5,
                    max_retries=1
                )
            )
            log.info("n8n_policy_change_webhook_triggered", brand_id=brand_id, url=url)
        except Exception as e:
            log.warning("n8n_policy_change_webhook_failed", error=str(e))

        return True

    except Exception as exc:
        log.error("change_detector_error", url=url, error=str(exc))
        # Default to True on errors to ensure we don't accidentally skip crawling
        return True
    finally:
        if conn:
            await conn.close()
