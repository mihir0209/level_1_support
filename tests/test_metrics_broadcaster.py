"""Tests for Real-time Live Metrics WebSocket Push Gateway."""

from __future__ import annotations

import asyncio
import pytest
from core.orchestration.metrics_broadcaster import (
    start_metrics_broadcaster,
    stop_metrics_broadcaster,
    _broadcast_loop,
)


@pytest.mark.asyncio
async def test_metrics_broadcaster_lifecycle():
    """Verify that start and stop cycle registers and cancels the asyncio Task correctly."""
    await start_metrics_broadcaster()
    # Check that another start is a no-op
    await start_metrics_broadcaster()
    await stop_metrics_broadcaster()


@pytest.mark.asyncio
async def test_metrics_broadcast_loop(monkeypatch):
    """Verify that the broadcast loop gathers API overview and outputs formatted JSON to the WS manager."""
    broadcasts = []

    async def mock_overview():
        return {
            "registered_brands": 3,
            "active_sessions": 2,
            "knowledge_graph": {"nodes_by_label": [{"label": "Order", "count": 10}]},
            "postgres_kb": {"brand_1": 5}
        }

    async def mock_relationships():
        return {"relationships": [{"type": "PURCHASED", "count": 8}]}

    def mock_jobs():
        return [{"id": "job_1", "next_run_time": "2026-07-05"}]

    async def mock_broadcast(payload):
        broadcasts.append(payload)

    monkeypatch.setattr("main.system_overview", mock_overview)
    monkeypatch.setattr("main.graph_relationships", mock_relationships)
    monkeypatch.setattr("ingestion.scheduler.list_jobs", mock_jobs)
    monkeypatch.setattr("main.ws_manager.broadcast", mock_broadcast)

    # Execute a single broadcast iteration by running the task for a split second and interrupting it
    task = asyncio.create_task(_broadcast_loop())
    await asyncio.sleep(2.5)  # Wait for initial sleep(2) to pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(broadcasts) >= 1
    p = broadcasts[0]
    assert p["event"] == "system_metrics"
    assert p["data"]["overview"]["registered_brands"] == 3
    assert p["data"]["relationships"][0]["type"] == "PURCHASED"
    assert p["data"]["jobs"][0]["id"] == "job_1"
