import os
import sys
import uuid
import json
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import app config so we can monkeypatch
from config import settings

@pytest.fixture(autouse=True)
def force_postgres_config(monkeypatch):
    """Ensure postgres settings are considered configured so the endpoint doesn't bypass db."""
    monkeypatch.setattr(settings, "postgres_host", "localhost")
    monkeypatch.setattr(settings, "postgres_user", "test_user")
    monkeypatch.setattr(settings, "postgres_password", "test_pass")
    monkeypatch.setattr(settings, "postgres_db", "test_db")
    monkeypatch.setattr(settings, "postgres_port", 5432)
    monkeypatch.setattr(settings, "admin_api_key", "test_admin_key")

@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    """Mock Redis to prevent connection errors."""
    class FakeRedis:
        async def ping(self):
            return True
        async def get(self, key):
            return None
        async def setex(self, key, ttl, value):
            pass
    fake = FakeRedis()
    monkeypatch.setattr("core.memory.session.get_redis", lambda: fake)
    return fake

@pytest.fixture(autouse=True)
def mock_scheduler(monkeypatch):
    """Prevent APScheduler from starting."""
    import ingestion.scheduler as sched
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(sched, "stop_scheduler", lambda: None)

@pytest.fixture
def mock_db_pool(monkeypatch):
    """Mocks the postgres async pool and returns (pool, conn, cursor) mocks."""
    mock_cursor = AsyncMock()
    mock_conn = AsyncMock()
    mock_pool = MagicMock()

    # Async context manager mock for pool.connection()
    class AsyncContextManagerMock:
        async def __aenter__(self):
            return mock_conn
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_pool.connection.return_value = AsyncContextManagerMock()
    mock_conn.execute.return_value = mock_cursor

    monkeypatch.setattr("core.db.pool.get_async_pool", lambda: mock_pool)
    return mock_pool, mock_conn, mock_cursor

@pytest.fixture
def mock_graph(monkeypatch):
    """Mocks FalkorDB graph client query."""
    mock_graph_client = MagicMock()
    mock_graph_instance = MagicMock()
    
    # get_graph_client() returns (success_bool, graph_client)
    monkeypatch.setattr("core.ontology.seed_data.get_graph_client", lambda: (True, mock_graph_instance))
    monkeypatch.setattr("main.get_graph_client", lambda: (True, mock_graph_instance))
    return mock_graph_instance

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        c.headers["X-ADMIN-KEY"] = "test_admin_key"
        yield c

def test_get_review_queue_empty(client, mock_db_pool):
    pool, conn, cursor = mock_db_pool

    # Setup database responses:
    # 1. Total count query (fetchone) returns [0]
    # 2. Page items query (fetchall) returns []
    cursor.fetchone.return_value = [0]
    cursor.fetchall.return_value = []

    response = client.get("/admin/review/queue")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0
    assert data["items"] == []

    # Verify query structure
    assert conn.execute.call_count == 2

def test_get_review_queue_with_items(client, mock_db_pool, mock_graph):
    pool, conn, cursor = mock_db_pool

    # Mock DB row
    item_id = uuid.uuid4()
    created_at = datetime(2026, 6, 27, 12, 0, 0)
    mock_row = (
        item_id,
        "boohoo_uk",
        "return_policy",
        "res_123",
        "policy updated",
        {"history": []},
        created_at
    )

    # Setup database responses
    cursor.fetchone.return_value = [1]
    cursor.fetchall.return_value = [mock_row]

    # Setup FalkorDB response
    mock_query_res = MagicMock()
    mock_query_res.result_set = [
        ("Return Policy Resolution", json.dumps([{"order": 1, "instruction": "Test step"}]), "escalate_l2")
    ]
    mock_graph.query.return_value = mock_query_res

    response = client.get("/admin/review/queue")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    item = data["items"][0]
    assert item["id"] == str(item_id)
    assert item["brand_id"] == "boohoo_uk"
    assert item["issue_type"] == "return_policy"
    assert item["resolution_tried"] == "res_123"
    assert item["resolution_details"] is not None
    assert item["resolution_details"]["name"] == "Return Policy Resolution"
    assert item["resolution_details"]["steps"] == [{"order": 1, "instruction": "Test step"}]
    assert item["resolution_details"]["escalation_trigger"] == "escalate_l2"

def test_approve_review_item_no_payload(client, mock_db_pool):
    pool, conn, cursor = mock_db_pool
    item_id = uuid.uuid4()

    # SELECT row mock
    cursor.fetchone.return_value = ("boohoo_uk", "return_policy", "res_123")

    response = client.post(f"/admin/review/{item_id}/approve")
    assert response.status_code == 200
    assert response.json() == {"status": "approved", "item_id": str(item_id)}

    # Check update SQL executed
    assert conn.execute.call_count == 2
    # Verify it updated postgres
    args1 = conn.execute.call_args_list[0][0]
    args2 = conn.execute.call_args_list[1][0]
    assert "SELECT" in args1[0]
    assert "UPDATE" in args2[0]

def test_approve_review_item_with_payload(client, mock_db_pool, mock_graph):
    pool, conn, cursor = mock_db_pool
    item_id = uuid.uuid4()

    # SELECT row mock
    cursor.fetchone.return_value = ("boohoo_uk", "return_policy", "res_123")

    payload = {
        "name": "Updated Resolution",
        "steps": [
            {"order": 1, "instruction": "Step 1", "tool_required": "check_order", "condition": None}
        ],
        "escalation_trigger": "escalate_agent"
    }

    # Mock graph response
    mock_query_res = MagicMock()
    mock_graph.query.return_value = mock_query_res

    response = client.post(f"/admin/review/{item_id}/approve", json=payload)
    assert response.status_code == 200
    assert response.json() == {"status": "approved", "item_id": str(item_id)}

    # Assert graph query called to merge/update the Resolution node
    mock_graph.query.assert_called_once()
    cypher_call = mock_graph.query.call_args[0][0]
    assert "MERGE (r:Resolution" in cypher_call
    params = mock_graph.query.call_args[0][1]
    assert params["res_id"] == "res_123"
    assert params["name"] == "Updated Resolution"
    assert params["escalation_trigger"] == "escalate_agent"

def test_reject_review_item(client, mock_db_pool):
    pool, conn, cursor = mock_db_pool
    item_id = uuid.uuid4()

    # SELECT row mock
    cursor.fetchone.return_value = (item_id,)

    response = client.post(f"/admin/review/{item_id}/reject", json={"reason": "not relevant"})
    assert response.status_code == 200
    assert response.json() == {"status": "rejected", "item_id": str(item_id), "reason": "not relevant"}

    # Verified update reviewed status
    assert conn.execute.call_count == 2
    args2 = conn.execute.call_args_list[1][0]
    assert "UPDATE" in args2[0]
    assert "reviewed = true" in args2[0]

def test_edit_review_item(client, mock_db_pool, mock_graph):
    pool, conn, cursor = mock_db_pool
    item_id = uuid.uuid4()

    cursor.fetchone.return_value = ("return_policy", "res_123")

    payload = {
        "name": "Direct Edited Resolution",
        "steps": [
            {"order": 1, "instruction": "Step 1", "tool_required": None, "condition": None}
        ],
        "escalation_trigger": None
    }

    # Mock graph response
    mock_query_res = MagicMock()
    mock_graph.query.return_value = mock_query_res

    response = client.post(f"/admin/review/{item_id}/edit", json=payload)
    assert response.status_code == 200
    assert response.json() == {"status": "edited", "resolution_id": "res_123"}

    mock_graph.query.assert_called_once()
    cypher_call = mock_graph.query.call_args[0][0]
    assert "MERGE (r:Resolution" in cypher_call

def test_review_item_not_found(client, mock_db_pool):
    pool, conn, cursor = mock_db_pool
    item_id = uuid.uuid4()

    # SELECT row mock returns None (not found)
    cursor.fetchone.return_value = None

    response = client.post(f"/admin/review/{item_id}/approve")
    assert response.status_code == 404

def test_unauthorized_access(client):
    client.headers.pop("X-ADMIN-KEY", None)
    response = client.get("/admin/review/queue")
    assert response.status_code == 401
