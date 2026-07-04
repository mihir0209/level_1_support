"""Background Metrics Broadcaster for Supervisor Dashboard."""

from __future__ import annotations

import asyncio
import structlog

log = structlog.get_logger(__name__)

_broadcaster_task: asyncio.Task | None = None


async def start_metrics_broadcaster() -> None:
    """Start the periodic background metrics broadcaster loop."""
    global _broadcaster_task
    if _broadcaster_task is not None:
        log.warning("metrics_broadcaster_already_running")
        return

    _broadcaster_task = asyncio.create_task(_broadcast_loop())
    log.info("metrics_broadcaster_started")


async def stop_metrics_broadcaster() -> None:
    """Stop the periodic background metrics broadcaster loop."""
    global _broadcaster_task
    if _broadcaster_task is None:
        return

    _broadcaster_task.cancel()
    try:
        await _broadcaster_task
    except asyncio.CancelledError:
        pass
    _broadcaster_task = None
    log.info("metrics_broadcaster_stopped")


async def _broadcast_loop() -> None:
    """Infinite loop gathering and pushing system overview metrics via websockets."""
    # Give the app a moment to spin up fully
    await asyncio.sleep(2)
    while True:
        try:
            from main import system_overview, graph_relationships, ws_manager
            from ingestion.scheduler import list_jobs

            overview = await system_overview()
            rels = await graph_relationships()
            jobs = list_jobs()

            payload = {
                "event": "system_metrics",
                "data": {
                    "overview": overview,
                    "relationships": rels.get("relationships", []),
                    "jobs": jobs
                }
            }
            # Broadcast to all connected websocket clients
            await ws_manager.broadcast(payload)
        except Exception as e:
            log.error("metrics_broadcast_failed", error=str(e))

        await asyncio.sleep(10)
