"""
LangGraph state machine — the orchestration core.

Nodes:
  assemble_context   → Context Engineering Engine (8-step pipeline)
  route_to_agent     → Select specialist agent via Swarms HierarchicalSwarm
  execute_tools      → Run tool calls returned by the specialist
  generate_response  → Main LLM call with full ContextPackage
  check_resolution   → Decide: next turn | escalate | close
  escalate_session   → Create Chatwoot ticket, human handoff
  close_session      → Log outcome, update graph, trigger feedback loop

Edge routing:
  START → assemble_context → route_to_agent
  route_to_agent → execute_tools  (if tools needed)
  route_to_agent → generate_response  (direct response)
  execute_tools  → generate_response
  generate_response → check_resolution
  check_resolution → [await_next_turn | escalate | close]
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from config import settings
from core.context.engine import assemble_context
from core.knowledge.retriever import update_resolution_success_rate
from core.ontology.models import Channel, Customer, SessionOutcome
from core.orchestration.llm import get_llm_client
from core.orchestration.state import L1SupportState
from core.tools.registry import ALL_TOOLS, TOOL_REGISTRY

log = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# ESCALATION THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────
MAX_RESOLUTION_ATTEMPTS = 10
MAX_TURNS = 10
LOW_CONFIDENCE_THRESHOLD = 0.45

# ─────────────────────────────────────────────────────────────────────────
# NODE: assemble_context
# ─────────────────────────────────────────────────────────────────────────

async def node_assemble_context(state: L1SupportState) -> dict[str, Any]:
    """
    Step 1-8 of the Context Engineering pipeline.
    Runs on every turn. Returns updated state fields.
    """
    llm = get_llm_client()

    # Extract last user message
    last_user_message = ""
    for msg in reversed(state.messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_message = msg.get("content", "")
            break
        elif isinstance(msg, HumanMessage):
            last_user_message = msg.content
            break

    # Build conversation history list for context engine
    history = [
        {"role": "user" if isinstance(m, HumanMessage) else "assistant",
         "content": m.content if hasattr(m, "content") else str(m)}
        for m in state.messages[:-1]  # exclude current message (already in last_user_message)
        if not isinstance(m, ToolMessage)
    ]

    # Get order date for temporal policy lookup (from first order if available)
    order_placed_at = None
    if state.customer and state.customer.recent_orders:
        order_placed_at = state.customer.recent_orders[0].placed_at

    context_package = await assemble_context(
        message=last_user_message,
        session_id=state.session_id,
        brand_id=state.brand_id,
        channel=state.channel,
        conversation_history=history,
        customer=state.customer,
        llm_client=llm,
        customer_order_placed_at=order_placed_at,
    )

    log.info(
        "context_assembled",
        session_id=state.session_id,
        intent=context_package.intent.issue_type if context_package.intent else "unknown",
        latency_ms=round(context_package.assembly_latency_ms, 1),
    )

    intent_conf = context_package.intent.confidence if context_package.intent else 0.0
    effective_conf = max(intent_conf, context_package.retrieval_confidence)

    return {
        "context_package": context_package,
        "intent": context_package.intent,
        "entities": context_package.entities,
        "confidence": effective_conf,
        "turn_count": state.turn_count + 1,
    }


# ─────────────────────────────────────────────────────────────────────────
# NODE: route_to_agent
# ─────────────────────────────────────────────────────────────────────────

# Intent → specialist agent name mapping
_INTENT_TO_AGENT = {
    "wismo":             "wismo_agent",
    "return_initiate":   "returns_agent",
    "refund_delay":      "refund_agent",
    "wrong_item":        "returns_agent",
    "damaged_item":      "returns_agent",
    "order_cancel":      "wismo_agent",
    "payment_failed":    "payment_agent",
    "promo_not_applied": "promos_agent",
    "klarna_return":     "returns_agent",
    "afterpay_return":   "returns_agent",
    "size_query":        "product_agent",
    "account_locked":    "account_agent",
    "delivery_query":    "wismo_agent",
    "unknown":           "supervisor_agent",
}


async def node_route_to_agent(state: L1SupportState) -> dict[str, Any]:
    """
    Determine which specialist agent handles this intent.
    Attaches the routed agent name to state.
    The actual response generation happens in node_generate_response.
    """
    intent_id = state.intent.issue_type if state.intent else "unknown"
    agent_name = _INTENT_TO_AGENT.get(intent_id, "supervisor_agent")

    log.info(
        "routed_to_agent",
        session_id=state.session_id,
        intent=intent_id,
        agent=agent_name,
    )
    return {"routed_to_agent": agent_name}


# ─────────────────────────────────────────────────────────────────────────
# NODE: generate_response
# ─────────────────────────────────────────────────────────────────────────

def _build_llm_messages(state: L1SupportState) -> list:
    """
    Construct the message list passed to the LLM.
    Injects ContextPackage blocks as the system message.
    """
    ctx = state.context_package
    if not ctx:
        return [HumanMessage(content="Hello, I need help.")]

    # Build structured system message from ContextPackage blocks
    system_content = "\n\n".join([
        ctx.system_prompt,
        "---",
        ctx.user_context_block,
        "---",
        ctx.policy_block,
        "---",
        ctx.resolution_block,
        "---",
        f"AVAILABLE TOOLS: {', '.join(ctx.tool_manifest)}",
        "---",
        "CURRENT SESSION CONTEXT:",
        ctx.session_block,
        "---",
        "ESCALATION RULES: Escalate if: (1) resolution_attempts >= 10, "
        "(2) customer explicitly asks for a human agent, "
        "(3) refund is overdue (>21 days since return was received), "
        "(4) confidence in resolution is below 45%.",
        "---",
        "RESOLUTION SIGNAL: When the customer's issue is resolved, your response MUST end with the line:",
        "[RESOLVED]",
        "Do NOT add this signal unless the issue is genuinely resolved. "
        "If the customer thanks you or confirms they're satisfied, the issue is resolved.",
    ])

    messages = [SystemMessage(content=system_content)]

    # Append conversation history (avoid re-adding the system prompt's session context)
    for msg in state.messages:
        if isinstance(msg, (HumanMessage, AIMessage, ToolMessage)):
            messages.append(msg)
        elif isinstance(msg, dict):
            role = msg.get("role")
            if role == "user":
                messages.append(HumanMessage(content=msg["content"]))
            elif role == "assistant":
                messages.append(AIMessage(content=msg["content"]))

    return messages


async def node_generate_response(state: L1SupportState) -> dict[str, Any]:
    """
    Main LLM call. Uses the primary model with tools bound.
    Returns the AI response (with optional tool calls) appended to messages.
    """
    llm = get_llm_client()
    lc_client = llm.get_lc_client(fast=False)

    # Bind only the tools relevant to this intent
    available_tool_names = state.context_package.tool_manifest if state.context_package else []
    available_tools = [t for name, t in TOOL_REGISTRY.items() if name in available_tool_names]

    if available_tools:
        model_with_tools = lc_client.bind_tools(available_tools)
    else:
        model_with_tools = lc_client

    messages = _build_llm_messages(state)

    try:
        response = await model_with_tools.ainvoke(messages)
    except Exception as e:
        log.error("llm_invoke_failed", session_id=state.session_id, error=str(e))
        response = AIMessage(
            content="I'm experiencing a technical issue. Let me connect you with a member of our team.",
        )

    log.info(
        "response_generated",
        session_id=state.session_id,
        agent=state.routed_to_agent,
        has_tool_calls=bool(getattr(response, "tool_calls", None)),
    )

    updates: dict[str, Any] = {"messages": [response]}

    # If no tool calls — this is a direct text response, increment attempt counter
    if not getattr(response, "tool_calls", None):
        updates["resolution_attempts"] = state.resolution_attempts + 1
        if state.context_package and state.context_package.intent:
            updates["current_resolution_id"] = (
                state.context_package.intent.issue_type  # placeholder, refined in Phase 2
            )

    return updates


# ─────────────────────────────────────────────────────────────────────────
# NODE: check_resolution  (routing decision node)
# ─────────────────────────────────────────────────────────────────────────

def check_resolution_routing(state: L1SupportState) -> Literal["tools", "escalate", "close", "continue"]:
    """
    Conditional edge: decide what happens after generate_response.

    Returns:
      "tools"    → there are tool calls to execute
      "escalate" → we should create a human ticket
      "close"    → session is resolved
      "continue" → wait for next customer message (happy path)
    """
    last_ai_message = None
    for msg in reversed(state.messages):
        if isinstance(msg, AIMessage):
            last_ai_message = msg
            break

    # If model made tool calls → execute them
    if last_ai_message and getattr(last_ai_message, "tool_calls", None):
        return "tools"

    # Escalation conditions
    content = (last_ai_message.content or "").lower() if last_ai_message else ""
    explicit_escalation = any(phrase in content for phrase in [
        "connect you with", "human agent", "member of our team", "specialist",
        "ticket_id"
    ])

    if explicit_escalation:
        return "escalate"
    if state.resolution_attempts >= MAX_RESOLUTION_ATTEMPTS:
        return "escalate"
    if state.turn_count >= MAX_TURNS:
        return "escalate"
    if state.confidence < LOW_CONFIDENCE_THRESHOLD and state.turn_count > 1:
        return "escalate"

    # No tool calls available AND confidence too low to self-resolve on first response:
    # escalate immediately rather than looping with unhelpful replies.
    tool_manifest = (state.context_package.tool_manifest if state.context_package else [])
    if not tool_manifest and state.confidence < LOW_CONFIDENCE_THRESHOLD:
        return "escalate"

    # Agent explicitly marked as resolved via [RESOLVED] signal in response
    if "[resolved]" in content:
        return "close"

    return "continue"


# ─────────────────────────────────────────────────────────────────────────
# GRAPHITI EPISODE BUILDER
# ─────────────────────────────────────────────────────────────────────────

def _build_episode_text(state: L1SupportState, outcome: str, extra: str = "") -> str:
    """Compose a human-readable episode summary for Graphiti ingestion."""
    intent_str = state.intent.issue_type if state.intent else "unknown"
    category = state.intent.category.value if state.intent else "unknown"
    entities = state.entities or {}

    parts = [
        f"Customer contacted support via {state.channel.value} about {intent_str} ({category}).",
        f"Session lasted {state.turn_count} turn(s).",
        f"Outcome: {outcome}.",
    ]
    if entities.get("order_id"):
        parts.append(f"Order referenced: {entities['order_id']}.")
    if entities.get("return_reason"):
        parts.append(f"Return reason: {entities['return_reason']}.")
    if entities.get("item_description"):
        parts.append(f"Item: {entities['item_description']}.")
    if state.current_resolution_id and outcome == "resolved":
        parts.append(f"Resolution applied: {state.current_resolution_id}.")
    if extra:
        parts.append(extra)
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# NODE: escalate_session
# ─────────────────────────────────────────────────────────────────────────

async def node_escalate(state: L1SupportState) -> dict[str, Any]:
    """
    Escalate to human agent. Creates Chatwoot ticket (Phase 1: mock).
    Writes escalation episode to Graphiti Tier 3 memory.
    """
    reason = state.escalation_reason or f"Auto-escalate after {state.resolution_attempts} attempt(s)"
    log.info(
        "session_escalated",
        session_id=state.session_id,
        brand_id=state.brand_id,
        reason=reason,
        turn_count=state.turn_count,
    )

    # Tier 3: write escalation episode so future agents know this customer was escalated
    if state.customer_id:
        try:
            from core.memory.graphiti_client import graphiti_memory
            episode = _build_episode_text(state, outcome="escalated", extra=reason)
            await graphiti_memory.add_episode(
                customer_id=state.customer_id,
                brand_id=state.brand_id,
                content=episode,
                metadata={
                    "channel": state.channel.value,
                    "outcome": "escalated",
                    "reason": reason[:200],
                    "intent": state.intent.issue_type if state.intent else "unknown",
                },
                episode_name=f"support_escalated:{state.session_id}",
            )
            log.info("graphiti_episode_written", session_id=state.session_id, outcome="escalated")
        except Exception as e:
            log.warning("graphiti_write_failed", session_id=state.session_id, error=str(e))

    return {
        "is_resolved": False,
        "outcome": SessionOutcome.ESCALATED,
        "escalation_reason": reason,
    }


# ─────────────────────────────────────────────────────────────────────────
# NODE: close_session
# ─────────────────────────────────────────────────────────────────────────

async def node_close_session(state: L1SupportState) -> dict[str, Any]:
    """
    Session resolved. Trigger feedback loop — update Resolution.successRate in graph
    and write episode to Graphiti Tier 3 memory.
    """
    log.info(
        "session_closed",
        session_id=state.session_id,
        brand_id=state.brand_id,
        outcome="resolved",
        turns=state.turn_count,
    )

    # Feedback loop: update graph
    if state.current_resolution_id:
        try:
            update_resolution_success_rate(
                resolution_id=state.current_resolution_id,
                resolved=True,
                satisfaction=state.satisfaction or 0.75,
            )
        except Exception as e:
            log.warning("feedback_loop_failed", error=str(e))

    # Tier 3: write resolved episode to Graphiti
    if state.customer_id:
        try:
            from core.memory.graphiti_client import graphiti_memory
            intent_str = state.intent.issue_type if state.intent else "unknown"
            episode = _build_episode_text(state, outcome="resolved")
            await graphiti_memory.add_episode(
                customer_id=state.customer_id,
                brand_id=state.brand_id,
                content=episode,
                metadata={
                    "channel": state.channel.value,
                    "outcome": "resolved",
                    "intent": intent_str,
                    "turns": state.turn_count,
                },
                episode_name=f"support_resolved:{state.session_id}",
            )
            log.info("graphiti_episode_written", session_id=state.session_id, outcome="resolved")
        except Exception as e:
            log.warning("graphiti_write_failed", session_id=state.session_id, error=str(e))

    return {
        "is_resolved": True,
        "outcome": SessionOutcome.RESOLVED,
    }


# ─────────────────────────────────────────────────────────────────────────
# GRAPH CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """
    Assemble and compile the LangGraph state machine.
    Returns a compiled graph ready for .invoke() / .ainvoke().
    """
    builder = StateGraph(L1SupportState)

    # ── Add nodes ────────────────────────────────────────────────────────
    builder.add_node("assemble_context", node_assemble_context)
    builder.add_node("route_to_agent", node_route_to_agent)
    builder.add_node("generate_response", node_generate_response)
    builder.add_node("tools", ToolNode(ALL_TOOLS))      # LangGraph's built-in tool executor
    builder.add_node("escalate", node_escalate)
    builder.add_node("close", node_close_session)

    # ── Add edges ─────────────────────────────────────────────────────────
    builder.add_edge(START, "assemble_context")
    builder.add_edge("assemble_context", "route_to_agent")
    builder.add_edge("route_to_agent", "generate_response")

    # Conditional routing after response generation
    builder.add_conditional_edges(
        "generate_response",
        check_resolution_routing,
        {
            "tools":    "tools",
            "escalate": "escalate",
            "close":    "close",
            "continue": END,      # wait for next user message
        },
    )

    # After tool execution, re-generate response with tool results
    builder.add_edge("tools", "generate_response")

    # Terminal nodes
    builder.add_edge("escalate", END)
    builder.add_edge("close", END)

    return builder.compile()


# Module-level compiled graph singleton
_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
