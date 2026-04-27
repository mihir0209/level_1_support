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

log = structlog.get_logger(__name__)


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

    async def run(self, task: str) -> str:
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


async def run_specialist(
    agent: Any,
    task: str,
    brand_id: str,
    customer_id: str | None = None,
) -> str:
    """
    Run a specialist agent on a task string.

    Dynamic tools are loaded from the registered API registry and injected into
    the agent at runtime, giving it real customer/order/return data for this brand.
    """
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
        result = await agent.run(full_task)
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
