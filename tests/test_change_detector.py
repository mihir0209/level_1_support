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

    # Scenario 1: New Page Source (fetchrow returns None)
    mock_conn.fetchrow = AsyncMock(return_value=None)
    
    changed = await detect_change_and_update(brand_id, url, initial_content)
    assert changed is True
    
    # Assert query to insert new row was made
    insert_queries = [q for q, a in queries_run if "INSERT" in q]
    assert len(insert_queries) == 1
    assert "crawl_sources" in insert_queries[0]
    queries_run.clear()

    # Scenario 2: Unchanged Content (fetchrow returns the same hash)
    mock_conn.fetchrow = AsyncMock(return_value={"last_hash": calculate_hash(initial_content)})
    
    changed = await detect_change_and_update(brand_id, url, initial_content)
    assert changed is False
    
    # Assert update query (to update last_crawled_at only) was executed
    update_queries = [q for q, a in queries_run if "UPDATE" in q]
    assert len(update_queries) == 1
    assert "last_crawled_at" in update_queries[0]
    queries_run.clear()

    # Scenario 3: Changed Content (fetchrow returns a different hash)
    mock_conn.fetchrow = AsyncMock(return_value={"last_hash": calculate_hash(initial_content)})
    
    changed = await detect_change_and_update(brand_id, url, modified_content)
    assert changed is True
    
    # Assert update query (to update last_hash and last_crawled_at) was executed
    update_queries = [q for q, a in queries_run if "UPDATE" in q]
    assert len(update_queries) == 1
    assert "last_hash" in update_queries[0]
