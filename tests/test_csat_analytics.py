"""Tests for Multi-Brand CSAT Dashboard & Analytics Report Export (Phase 30)."""

from __future__ import annotations

import io
import csv
import json
import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.mark.asyncio
async def test_csat_analytics_endpoint(monkeypatch):
    """Verify GET /admin/analytics/csat aggregates brand and intent stats correctly."""
    # Mock pool and execute query results
    brand_data = [
        ("boohoo_uk", 10, 4.25, 2),
        ("boohoo_us", 5, 4.80, 0)
    ]
    intent_data = [
        ("return_request", 8, 3.5),
        ("order_tracking", 7, 4.9)
    ]

    class MockConn:
        async def execute(self, query, params=None):
            class MockResult:
                def __init__(self, data):
                    self.data = data
                async def fetchall(self):
                    return self.data
            
            if "GROUP BY brand_id" in query:
                return MockResult(brand_data)
            else:
                return MockResult(intent_data)

    class MockPool:
        def connection(self):
            class AsyncContext:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return AsyncContext()

    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: MockPool())
    monkeypatch.setattr("config.settings.postgres_host", "localhost")

    client = TestClient(app)
    response = client.get("/admin/analytics/csat", headers={"X-Admin-Key": "l1support-demo-key-2026"})
    assert response.status_code == 200
    
    data = response.json()
    assert "brand_stats" in data
    assert "intent_stats" in data
    
    assert data["brand_stats"][0]["brand_id"] == "boohoo_uk"
    assert data["brand_stats"][0]["avg_csat"] == 4.25
    assert data["brand_stats"][0]["low_csat_count"] == 2
    
    assert data["intent_stats"][0]["intent"] == "return_request"
    assert data["intent_stats"][0]["avg_csat"] == 3.5


@pytest.mark.asyncio
async def test_csat_export_csv(monkeypatch):
    """Verify exporting raw CSAT survey logs as CSV."""
    import uuid
    from datetime import datetime, timezone

    uid_sess = uuid.uuid4()
    mock_rows = [
        (uid_sess, "boohoo_uk", "cust_1", "chat", "order_tracking", "resolved", 3, 5.0, datetime.now(timezone.utc), datetime.now(timezone.utc))
    ]

    class MockConn:
        async def execute(self, query, params=None):
            class MockResult:
                async def fetchall(self):
                    return mock_rows
            return MockResult()

    class MockPool:
        def connection(self):
            class AsyncContext:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return AsyncContext()

    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: MockPool())
    monkeypatch.setattr("config.settings.postgres_host", "localhost")

    client = TestClient(app)
    response = client.get("/admin/analytics/csat/export?format=csv", headers={"X-Admin-Key": "l1support-demo-key-2026"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert "csat_export.csv" in response.headers["content-disposition"]

    content = response.content.decode("utf-8")
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)
    
    # Header row + data row = 2 rows
    assert len(rows) == 2
    assert rows[0][0] == "session_id"
    assert rows[1][0] == str(uid_sess)
    assert rows[1][1] == "boohoo_uk"
    assert rows[1][7] == "5.0"


@pytest.mark.asyncio
async def test_csat_export_jsonl(monkeypatch):
    """Verify exporting raw CSAT survey logs as JSONL."""
    import uuid
    from datetime import datetime, timezone

    uid_sess = uuid.uuid4()
    mock_rows = [
        (uid_sess, "boohoo_us", "cust_2", "chat", "refund", "resolved", 2, 4.0, datetime.now(timezone.utc), datetime.now(timezone.utc))
    ]

    class MockConn:
        async def execute(self, query, params=None):
            class MockResult:
                async def fetchall(self):
                    return mock_rows
            return MockResult()

    class MockPool:
        def connection(self):
            class AsyncContext:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return AsyncContext()

    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: MockPool())
    monkeypatch.setattr("config.settings.postgres_host", "localhost")

    client = TestClient(app)
    response = client.get("/admin/analytics/csat/export?format=jsonl", headers={"X-Admin-Key": "l1support-demo-key-2026"})
    assert response.status_code == 200
    assert "application/x-jsonlines" in response.headers["content-type"]
    assert "csat_export.jsonl" in response.headers["content-disposition"]

    content = response.content.decode("utf-8")
    lines = content.strip().split("\n")
    assert len(lines) == 1
    
    data = json.loads(lines[0])
    assert data["session_id"] == str(uid_sess)
    assert data["brand_id"] == "boohoo_us"
    assert data["satisfaction"] == 4.0
