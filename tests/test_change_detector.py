import pytest
from unittest.mock import AsyncMock, MagicMock
from ingestion.change_detector import detect_change_and_update, calculate_hash

@pytest.mark.asyncio
async def test_detect_change_and_update(monkeypatch):
    brand_id = "boohoo_uk"
    url = "https://boohoo.com/help/returns"
    initial_content = "Return window: 28 days"
    modified_content = "Return window: 14 days"

    # Set up mock DB connection
    mock_conn = AsyncMock()
    
    # Track the call counts for DB execution
    queries_run = []
    
    async def mock_execute(query, *args):
        queries_run.append((query, args))
        return "INSERT 0 1"
        
    mock_conn.execute = mock_execute

    # Mock asyncpg.connect
    async def mock_connect(dsn):
        return mock_conn
    monkeypatch.setattr("asyncpg.connect", mock_connect)

    # Mock HTTP requests and event loop to run synchronously in tests
    mock_http_request = MagicMock()
    monkeypatch.setattr("core.utils.http.http_request", mock_http_request)

    mock_loop = MagicMock()
    mock_loop.run_in_executor = lambda executor, func, *args: func(*args)
    monkeypatch.setattr("asyncio.get_running_loop", lambda: mock_loop)

    # Scenario 1: New Page Source (fetchrow returns None)
    mock_conn.fetchrow = AsyncMock(return_value=None)
    
    changed = await detect_change_and_update(brand_id, url, initial_content)
    assert changed is True
    
    # Assert query to insert new row was made
    insert_queries = [q for q, a in queries_run if "INSERT" in q]
    assert len(insert_queries) == 1
    assert "crawl_sources" in insert_queries[0]
    queries_run.clear()
    assert mock_http_request.call_count == 0

    # Scenario 2: Unchanged Content (fetchrow returns the same hash)
    mock_conn.fetchrow = AsyncMock(return_value={"last_hash": calculate_hash(initial_content)})
    
    changed = await detect_change_and_update(brand_id, url, initial_content)
    assert changed is False
    
    # Assert update query (to update last_crawled_at only) was executed
    update_queries = [q for q, a in queries_run if "UPDATE" in q]
    assert len(update_queries) == 1
    assert "last_crawled_at" in update_queries[0]
    queries_run.clear()
    assert mock_http_request.call_count == 0

    # Scenario 3: Changed Content (fetchrow returns a different hash)
    mock_conn.fetchrow = AsyncMock(return_value={"last_hash": calculate_hash(initial_content)})
    
    changed = await detect_change_and_update(brand_id, url, modified_content)
    assert changed is True
    
    # Assert update query (to update last_hash and last_crawled_at) was executed
    update_queries = [q for q, a in queries_run if "UPDATE" in q]
    assert len(update_queries) == 1
    assert "last_hash" in update_queries[0]

    # Verify n8n webhook was triggered on change
    mock_http_request.assert_called_once()
    args, kwargs = mock_http_request.call_args
    assert args[0] == "POST"
    assert "/webhook/policy-change" in args[1]
    assert kwargs["json_body"]["brand_id"] == brand_id
    assert kwargs["json_body"]["url"] == url
