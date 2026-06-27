import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage

from core.ontology.models import Channel, IntentResult, SessionOutcome, Customer
from core.taxonomy import SupportCategory
from core.orchestration.state import L1SupportState
from core.orchestration.graph import node_escalate
from config import settings

@pytest.mark.asyncio
async def test_node_escalate_triggers_n8n_webhook(monkeypatch):
    session_id = "test-session-123"
    brand_id = "boohoo_uk"
    
    # 1. Setup mock session data
    mock_session_data = {
        "chatwoot_conv_id": "456",
        "brand_id": brand_id,
    }
    
    async def mock_get_session(sid):
        assert sid == session_id
        return mock_session_data
        
    monkeypatch.setattr("core.memory.session.get_session", mock_get_session)
    
    # 2. Mock Chatwoot and FalkorDB client functions to avoid live calls
    mock_post_message = MagicMock()
    mock_assign_team = MagicMock()
    mock_add_label = MagicMock()
    
    monkeypatch.setattr("channels.chat.chatwoot_client.post_message", mock_post_message)
    monkeypatch.setattr("channels.chat.chatwoot_client.assign_team", mock_assign_team)
    monkeypatch.setattr("channels.chat.chatwoot_client.add_label", mock_add_label)
    
    # Mock MemPalace and Graphiti to avoid live database writes
    monkeypatch.setattr("core.memory.mempalace_client.mempalace.remember", MagicMock())
    
    async def mock_add_episode(**kwargs):
        pass
    monkeypatch.setattr("core.memory.graphiti_client.graphiti_memory.add_episode", mock_add_episode)
    
    # 3. Mock http_request and event loop run_in_executor
    mock_http_request = MagicMock()
    monkeypatch.setattr("core.utils.http.http_request", mock_http_request)
    
    mock_loop = MagicMock()
    mock_loop.run_in_executor = lambda executor, func, *args: func(*args)
    monkeypatch.setattr("asyncio.get_event_loop", lambda: mock_loop)
    
    # 4. Create L1SupportState
    messages = [
        HumanMessage(content="My order hasn't arrived!"),
        AIMessage(content="Let me check that for you.")
    ]
    
    customer = Customer(
        id="cust-123",
        brand_id=brand_id,
        email="customer@example.com"
    )
    
    state = L1SupportState(
        session_id=session_id,
        brand_id=brand_id,
        channel=Channel.CHAT,
        customer_id="cust-123",
        messages=messages,
        turn_count=2,
        intent=IntentResult(
            issue_type="wismo",
            confidence=0.9,
            category=SupportCategory.SERVICE_STATUS,
            entities={}
        ),
        customer=customer,
        escalation_reason="customer dissatisfied with automated flow",
        outcome=SessionOutcome.ESCALATED
    )
    
    # 5. Run node_escalate
    result = await node_escalate(state)
    
    # 6. Verify result outcome
    assert result["outcome"] == SessionOutcome.ESCALATED
    assert result["chatwoot_conversation_id"] == "456"
    
    # 7. Assert n8n escalation webhook was triggered with correct payload
    mock_http_request.assert_called_once()
    args, kwargs = mock_http_request.call_args
    assert args[0] == "POST"
    assert "/webhook/escalation" in args[1]
    
    payload = kwargs["json_body"]
    assert payload["session_id"] == session_id
    assert payload["brand_id"] == brand_id
    assert payload["intent"] == "wismo"
    assert payload["reason"] == "customer dissatisfied with automated flow"
    assert "Customer: My order hasn't arrived!" in payload["transcript"]
    assert "Agent: Let me check that for you." in payload["transcript"]
    assert payload["chatwoot_conv_id"] == "456"
    assert payload["customer_id"] == "cust-123"
    assert payload["customer_email"] == "customer@example.com"
