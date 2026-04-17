"""
Base agent builder for specialist L1 support agents using Swarms.

Each specialist agent is a Swarms Agent configured with:
  - Domain-specific system prompt
  - Relevant tools from the tool registry
  - Provider-agnostic LLM via LiteLLM (respects LLM_PROVIDER)

run_specialist(agent, task, context) is the unified entry point.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

from config import settings

log = structlog.get_logger(__name__)


def _swarms_llm_kwargs() -> dict[str, Any]:
    """Return model_name and credentials for Swarms Agent, matching the active provider."""
    p = settings.llm_provider.lower()
    if p == "azure":
        return {
            "model_name": f"azure/{settings.azure_primary_deployment}",
            "llm_api_key": settings.azure_openai_api_key,
            "llm_base_url": settings.azure_openai_endpoint,
        }
    elif p == "openai":
        return {
            "model_name": settings.openai_primary_model,
            "llm_api_key": settings.openai_api_key,
        }
    else:  # anthropic
        return {
            "model_name": settings.primary_model,
            "llm_api_key": settings.anthropic_api_key,
        }


def build_agent(
    name: str,
    description: str,
    system_prompt: str,
    tools: list[Callable] | None = None,
    max_loops: int = 3,
) -> Any:
    """
    Build a configured Swarms Agent for a specialist domain.

    Returns a Swarms Agent instance ready to run tasks.
    """
    from swarms import Agent

    llm_kwargs = _swarms_llm_kwargs()

    agent = Agent(
        agent_name=name,
        agent_description=description,
        system_prompt=system_prompt,
        tools=tools or [],
        max_loops=max_loops,
        output_type="str-all-except-first",
        verbose=False,
        print_on=False,
        reasoning_enabled=False,
        **llm_kwargs,
    )
    log.debug("agent_built", name=name, provider=settings.llm_provider)
    return agent


async def run_specialist(
    agent: Any,
    task: str,
    brand_id: str,
    customer_id: str | None = None,
) -> str:
    """
    Run a specialist agent on a task string.

    Swarms agents are synchronous — run in executor to avoid blocking the event loop.
    Returns the agent's final response string.
    """
    import asyncio

    context_prefix = f"[Brand: {brand_id}]"
    if customer_id:
        context_prefix += f" [Customer: {customer_id}]"
    full_task = f"{context_prefix}\n\n{task}"

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: agent.run(full_task))
        return str(result) if result else ""
    except Exception as exc:
        log.error("specialist_agent_failed", agent=agent.agent_name, error=str(exc))
        raise
