"""
Tests for specialist agents (simple.py / base.py).

Verifies agent construction, intent routing, and basic agent behavior
without making actual LLM calls.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAgentConstruction:

    def test_all_intent_agents_buildable(self):
        """Each intent should resolve to a buildable agent (no LLM call needed)."""
        from core.agents.specialists import INTENT_TO_AGENT

        for intent, factory in INTENT_TO_AGENT.items():
            factory.cache_clear() if hasattr(factory, "cache_clear") else None
            agent = factory()
            assert agent is not None, f"Agent for intent {intent} should not be None"
            assert hasattr(agent, "name"), f"Agent for {intent} missing .name attribute"
            assert hasattr(agent, "run"), f"Agent for {intent} missing .run() method"

    def test_intent_coverage(self):
        """All expected issue types should map to an agent."""
        from core.agents.specialists import INTENT_TO_AGENT
        expected_intents = {
            "wismo", "return_initiate", "refund_delay", "wrong_item",
            "damaged_item", "order_cancel", "payment_failed", "klarna_return",
            "promo_not_applied", "size_query", "account_locked", "escalate",
        }
        for intent in expected_intents:
            assert intent in INTENT_TO_AGENT, f"Missing agent for intent: {intent}"

    def test_unknown_intent_falls_back_to_escalation(self):
        """Unknown intents should map to the escalation agent."""
        from core.agents.specialists import get_agent_for_intent, get_escalation_agent
        assert callable(get_agent_for_intent)
        assert callable(get_escalation_agent)
        # Clear caches so we get fresh instances
        get_escalation_agent.cache_clear() if hasattr(get_escalation_agent, "cache_clear") else None
        # Unknown intent returns escalation agent
        fallback = get_agent_for_intent("unknown_intent")
        expected = get_escalation_agent()
        assert type(fallback) == type(expected), "Unknown intent should fallback to escalation agent"


class TestSimpleAgent:

    def test_build_agent_returns_simple_agent(self):
        """build_agent() should return a SimpleAgent instance."""
        from core.agents.simple import build_agent

        agent = build_agent(
            name="TestAgent",
            description="Test description",
            system_prompt="You are a test agent.",
            tools=[],
            max_loops=2,
        )
        assert agent.name == "TestAgent"
        assert agent.max_loops == 2
        assert agent.description == "Test description"

    def test_agent_has_tools_attribute(self):
        """Agent should have tools list."""
        from core.tools.registry import order_lookup
        from core.agents.simple import build_agent

        agent = build_agent(
            name="ToolAgent",
            description="Agent with tools",
            system_prompt="Use tools.",
            tools=[order_lookup],
        )
        assert hasattr(agent, "tools")
        assert order_lookup in agent.tools

    def test_agent_run_accepts_string_task(self):
        """Agent.run() should accept a task string and return a coroutine."""
        from core.agents.simple import build_agent

        agent = build_agent(
            name="NoToolAgent",
            description="No tools agent",
            system_prompt="Answer briefly.",
            tools=[],
            max_loops=1,
        )
        # run() is async — verify it returns a coroutine
        import asyncio
        coro = agent.run("Hello")
        assert asyncio.iscoroutine(coro)
        # Clean up without awaiting (requires LLM to be configured)
        coro.close()

    def test_inject_dynamic_tools(self):
        """_inject_dynamic_tools should append tools without duplicates."""
        from core.tools.registry import order_lookup
        from core.agents.simple import build_agent, _inject_dynamic_tools

        agent = build_agent(
            name="TestAgent",
            description="Test",
            system_prompt="Test.",
            tools=[order_lookup],
        )
        initial_count = len(agent.tools)

        # Inject same tool — should not duplicate
        _inject_dynamic_tools(agent, [order_lookup])
        assert len(agent.tools) == initial_count

        # Inject different tool
        from core.tools.registry import shipment_track
        _inject_dynamic_tools(agent, [shipment_track])
        assert len(agent.tools) == initial_count + 1
        assert shipment_track in agent.tools
