"""
Simple async agent using the existing LLM client + LangChain tool binding.

Replaces the swarms dependency with a minimal ReAct-style agent that:
  - Calls the LLM with bound tools
  - Executes tool calls from the response
  - Loops until max_loops or a text response

Used by all specialist agents (WISMO, Returns, Refund, etc.)
"""

from __future__ import annotations

import asyncio
import structlog
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from core.orchestration.llm import get_llm_client
from core.tracing import traced

log = structlog.get_logger(__name__)


class HighRiskActionPaused(Exception):
    def __init__(self, tool_name: str, tool_args: dict, tool_call_id: str):
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_call_id = tool_call_id
        super().__init__(f"Action {tool_name} requires human approval.")


class HighRiskActionRejected(Exception):
    def __init__(self, tool_name: str, feedback: str | None = None):
        self.tool_name = tool_name
        self.feedback = feedback
        super().__init__(f"Action {tool_name} was rejected: {feedback}")


class SimpleAgent:
    """
    Minimal ReAct-style agent.

    Holds a system prompt and a list of LangChain tools.
    run() executes a task through the LLM with tool binding,
    running tool calls in a loop up to max_loops.
    """

    def __init__(
        self,
        name: str,
        description: str,
        system_prompt: str,
        tools: list[BaseTool] | None = None,
        max_loops: int = 3,
    ):
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.max_loops = max_loops

    def _tool_by_name(self, name: str) -> BaseTool | None:
        for t in self.tools:
            if t.name == name:
                return t
        return None

    @traced("agent.run")
    async def run(self, task: str, state: Any | None = None) -> str:
        """
        Execute the task through the LLM with tool binding.

        Loops:
          1. LLM generate (with tools bound) → AIMessage (may have tool_calls)
          2. Execute each tool call → ToolMessage
          3. Append all messages to history
          4. Repeat up to max_loops
          5. If no tool_calls, return AIMessage content as final response
        """
        llm = get_llm_client()
        lc_client = llm.get_lc_client(fast=False)

        # Bind tools to LLM
        bound_llm = lc_client
        if self.tools:
            bound_llm = lc_client.bind_tools(self.tools)

        messages: list = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=task),
        ]

        for i in range(self.max_loops):
            try:
                if self.tools:
                    response: AIMessage = await bound_llm.ainvoke(messages)  # type: ignore[assignment]
                else:
                    response = await lc_client.ainvoke(messages)  # type: ignore[assignment]

                # No tool calls → final text response
                if not getattr(response, "tool_calls", None):
                    return response.content or ""

                # Execute tool calls
                tool_messages: list[ToolMessage] = []
                for call in response.tool_calls:  # type: ignore[union-attr]
                    name = call.get("name", "")
                    args = call.get("args") or {}
                    tool_id = call.get("id", "")

                    # Check if high-risk and requires human approval
                    is_high_risk = name in ("return_init", "cancel_order", "order_cancel", "initiate_return")
                    if is_high_risk and state is not None:
                        if state.human_approval is None:
                            raise HighRiskActionPaused(name, args, tool_id)
                        elif state.human_approval == "rejected":
                            raise HighRiskActionRejected(name, state.human_feedback)
                        elif state.human_approval == "approved":
                            log.info("agent_executing_approved_high_risk_tool", tool_name=name)

                    tool = self._tool_by_name(name)
                    if not tool:
                        tool_messages.append(ToolMessage(
                            content=f"Tool '{name}' not found.",
                            tool_call_id=tool_id,
                            name=name,
                        ))
                        continue

                    try:
                        # Tools are sync — run in executor
                        result = await asyncio.get_event_loop().run_in_executor(
                            None, lambda t=tool, a=args: t.invoke(a)
                        )
                        content = result if isinstance(result, str) else str(result)
                    except Exception as exc:
                        content = f"Tool execution failed: {exc}"

                    tool_messages.append(ToolMessage(
                        content=content,
                        tool_call_id=tool_id,
                        name=name,
                    ))

                messages.append(response)
                messages.extend(tool_messages)

            except (HighRiskActionPaused, HighRiskActionRejected):
                raise
            except Exception as exc:
                log.warning(
                    "simple_agent_loop_error",
                    agent=self.name,
                    loop=i + 1,
                    error=str(exc),
                )
                # On error, return what we have so far
                if messages:
                    try:
                        fallback: AIMessage = await lc_client.ainvoke(messages)
                        return fallback.content or f"[Agent error: {exc}]"
                    except Exception:
                        return f"[Agent error: {exc}]"
                return f"[Agent error: {exc}]"

        # max_loops reached — return last response
        try:
            final: AIMessage = await lc_client.ainvoke(messages)
            return final.content or ""
        except Exception as exc:
            return f"[Max loops reached, agent error: {exc}]"


# ── Convenience factory ────────────────────────────────────────────────────────

def build_agent(
    name: str,
    description: str,
    system_prompt: str,
    tools: list | None = None,
    max_loops: int = 3,
) -> SimpleAgent:
    """Build a configured SimpleAgent (same signature as old swarms.build_agent)."""
    return SimpleAgent(
        name=name,
        description=description,
        system_prompt=system_prompt,
        tools=tools,  # type: ignore[arg-type]
        max_loops=max_loops,
    )


# Fallback specialist map — used when domain config has no mapping for an intent.
# Primary lookup is via domain config; this is the safety net.
_FALLBACK_SPECIALIST_MAP: dict[str, str] = {
    "wismo": "wismo_agent",
    "return_initiate": "returns_agent",
    "refund_delay": "refund_agent",
    "wrong_item": "wrong_item_agent",
    "damaged_item": "wrong_item_agent",
    "order_cancel": "returns_agent",
    "payment_failed": "payment_agent",
    "klarna_return": "refund_agent",
    "afterpay_return": "refund_agent",
    "promo_not_applied": "promos_agent",
    "size_query": "account_agent",
    "account_locked": "account_agent",
    "delivery_query": "wismo_agent",
    "info": "info_agent",
    "network_outage": "wismo_agent",
    "billing_dispute": "refund_agent",
    "device_issue": "wrong_item_agent",
}


@traced("agent.specialist.run")
async def run_specialist(
    agent: Any,
    task: str,
    brand_id: str,
    customer_id: str | None = None,
    intent_id: str | None = None,
    state: Any | None = None,
) -> str:
    """
    Run a specialist agent on a task string.

    Dynamic tools are loaded from the registered API registry and injected into
    the agent at runtime, giving it real customer/order/return data for this brand.

    The system prompt is generated dynamically from KG data + domain config,
    replacing any empty prompt passed at agent construction time.
    """
    from brands.registry import get_brand_by_id
    from core.context.prompts import generate_specialist_prompt
    from domains.loader import get_domain_config

    # Resolve domain from brand entry, then load domain config
    brand_entry = get_brand_by_id(brand_id)
    domain = brand_entry.domain if brand_entry else "ecommerce"
    domain_cfg = get_domain_config(domain)

    # Resolve intent_id → agent_id using domain config (falls back to hardcoded map)
    agent_id = domain_cfg.get_specialist_for(intent_id or "") or _FALLBACK_SPECIALIST_MAP.get(intent_id or "", "info_agent")

    # Build domain-aware context tokens from config
    excluded_cats = domain_cfg.excluded_item_categories or []
    tier_display = brand_entry.name if brand_entry else brand_id

    domain_hints = [
        f"Brand: {tier_display} ({brand_id})",
        f"Domain: {domain}",
    ]
    if excluded_cats:
        domain_hints.append(f"Non-returnable categories: {', '.join(excluded_cats)}")

    # Generate dynamic system prompt from KG data + domain config
    dynamic_prompt = generate_specialist_prompt(agent_id, brand_id, domain=domain)

    # Inject domain-specific rules into the system prompt
    domain_suffix = "\n".join(domain_hints)
    if dynamic_prompt:
        # Append domain context after the prompt's existing rules
        dynamic_prompt = dynamic_prompt.rstrip() + "\n\n" + domain_suffix

    # Inject into agent (SimpleAgent.system_prompt is writable at runtime)
    agent.system_prompt = dynamic_prompt

    # Inject dynamic brand-specific tools
    try:
        from core.tools.dynamic_builder import load_tools_for_brand
        dynamic_tools = load_tools_for_brand(brand_id)
        if dynamic_tools:
            _inject_dynamic_tools(agent, dynamic_tools)
    except Exception:
        pass  # No dynamic tools — proceed with static tools only

    context_prefix = f"[Brand: {brand_id}]"
    if customer_id:
        context_prefix += f" [Customer: {customer_id}]"
    full_task = f"{context_prefix}\n\n{task}"

    try:
        result = await agent.run(full_task, state=state)
        return str(result) if result else ""
    except Exception as exc:
        log.error("specialist_agent_failed", agent=agent.name, error=str(exc))
        raise


def _inject_dynamic_tools(agent: Any, tools: list) -> None:
    """
    Inject dynamic tools into a SimpleAgent at runtime.
    """
    existing = getattr(agent, "tools", []) or []
    tool_names = {getattr(t, "name", str(t)) for t in existing}
    for t in tools:
        name = getattr(t, "name", str(t))
        if name not in tool_names:
            existing.append(t)
            tool_names.add(name)
    agent.tools = existing
