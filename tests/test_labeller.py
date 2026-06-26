import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage

from feedback.labeller import run_intent_labeller
from feedback.engine import approve_intent_label

@pytest.mark.asyncio
async def test_run_intent_labeller(monkeypatch):
    queue_id = uuid.uuid4()
    brand_id = "boohoo_uk"
    raw_message = "where is my package? I ordered it 5 days ago"
    intent = "wismo"

    # Set up mock DB connection and cursor
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_cur.__enter__.return_value = mock_cur
    mock_conn.__enter__.return_value = mock_conn

    # 1. Mock fetchall for run_intent_labeller query: SELECT id, brand_id, raw_message
    mock_cur.fetchall.return_value = [(queue_id, brand_id, raw_message)]

    # 2. Mock fetchone for approve_intent_label SELECT query
    mock_cur.fetchone.return_value = (raw_message,)

    # Patch the DB connection functions
    monkeypatch.setattr("feedback.labeller._get_db_conn", lambda: mock_conn)
    monkeypatch.setattr("feedback.engine._get_db_conn", lambda: mock_conn)

    # Mock the LLM client
    mock_lc_client = MagicMock()
    mock_lc_client.ainvoke = AsyncMock(return_value=AIMessage(content="wismo"))

    class MockLLMClient:
        def get_lc_client(self, fast=False):
            return mock_lc_client

    monkeypatch.setattr("feedback.labeller.get_llm_client", lambda: MockLLMClient())

    # Run the labeller
    processed = await run_intent_labeller()
    assert processed == 1

    # Verify database updates were executed
    mock_cur.execute.assert_any_call("""
                    SELECT id, brand_id, raw_message
                    FROM intent_label_queue
                    WHERE reviewed = false
                """)
    # Verify the update query executed with correct values
    mock_cur.execute.assert_any_call("""
                        UPDATE intent_label_queue
                        SET suggested_label = %s
                        WHERE id = %s
                    """, ("wismo", queue_id))

    # Test approval syncing with FalkorDB
    mock_graph = MagicMock()
    mock_graph.query = MagicMock()
    monkeypatch.setattr("core.ontology.schema.get_graph_client", lambda: (None, mock_graph))

    success = approve_intent_label(str(queue_id), "wismo")
    assert success is True

    # Check FalkorDB query was triggered with the correct parameters
    mock_graph.query.assert_called_once()
    query_str = mock_graph.query.call_args[0][0]
    params = mock_graph.query.call_args[1] or mock_graph.query.call_args[0][1]
    assert "example_phrases" in query_str
    assert params["approved_label"] == "wismo"
    assert params["phrase"] == raw_message
