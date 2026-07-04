"""Multi-Model Fallback & Cost Optimization Coordinator."""

from __future__ import annotations

import json
import time
import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)

DEFAULT_CONFIG = {
    "primary_model": "openai/gpt-4o",
    "fallback_model": "anthropic/claude-3-5-sonnet",
    "rate_limit_mitigations": 0,
    "pricing": {
        "openai/gpt-4o": {"input": 0.000005, "output": 0.000015},
        "anthropic/claude-3-5-sonnet": {"input": 0.000003, "output": 0.000015},
        "ollama/llama3": {"input": 0.0, "output": 0.0}
    }
}


async def get_fallback_config() -> dict:
    """Retrieve active LLM fallback chain configurations."""
    r = get_redis()
    raw = await r.get("llm:fallback:config")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return DEFAULT_CONFIG


async def save_fallback_config(config: dict) -> None:
    """Save active LLM fallback chain configurations."""
    r = get_redis()
    await r.set("llm:fallback:config", json.dumps(config))
    log.info("llm_fallback_config_saved", config=config)


async def call_llm_with_fallback(
    messages: list[dict],
    session_id: str | None = None,
    force_fail_primary: bool = False
) -> dict:
    """
    Executes LLM chat completions with automated failover handling.
    Tracks estimated token counts and pricing costs.
    """
    config = await get_fallback_config()
    primary = config["primary_model"]
    fallback = config["fallback_model"]
    pricing = config["pricing"]

    chosen_model = primary
    fallback_active = False

    prompt_text = "\n".join([m.get("content", "") for m in messages])
    input_tokens = int(len(prompt_text.split()) * 1.3)

    if force_fail_primary:
        # Simulate primary failure (e.g. rate limit error 429)
        fallback_active = True
        chosen_model = fallback
        config["rate_limit_mitigations"] += 1
        await save_fallback_config(config)
        log.warning("llm_primary_failed_switching_to_fallback", primary=primary, fallback=fallback)

    # Simulated LLM completion content
    completion_text = f"Simulated response from {chosen_model} regarding support context."
    output_tokens = int(len(completion_text.split()) * 1.3)

    # Cost Estimation
    model_pricing = pricing.get(chosen_model, {"input": 0.0, "output": 0.0})
    cost = (input_tokens * model_pricing["input"]) + (output_tokens * model_pricing["output"])

    if session_id:
        r = get_redis()
        # Save cost per session
        await r.set(f"session:{session_id}:llm_cost", str(cost))
        # Accumulate global costs
        await r.incrbyfloat("llm:cumulative_costs", cost)
        if fallback_active:
            # Compare to primary model to calculate savings
            primary_pricing = pricing.get(primary, {"input": 0.0, "output": 0.0})
            primary_est_cost = (input_tokens * primary_pricing["input"]) + (output_tokens * primary_pricing["output"])
            savings = max(0.0, primary_est_cost - cost)
            await r.incrbyfloat("llm:cumulative_savings", savings)

    return {
        "model": chosen_model,
        "content": completion_text,
        "fallback_triggered": fallback_active,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 6)
    }


async def get_gateway_analytics() -> dict:
    """Retrieve cumulative API savings and rate-limit mitigation history."""
    r = get_redis()
    costs = float(await r.get("llm:cumulative_costs") or "0.0")
    savings = float(await r.get("llm:cumulative_savings") or "0.0")
    config = await get_fallback_config()

    return {
        "cumulative_costs_usd": round(costs, 5),
        "cumulative_savings_usd": round(savings, 5),
        "rate_limit_mitigations": config.get("rate_limit_mitigations", 0),
        "primary_model": config["primary_model"],
        "fallback_model": config["fallback_model"]
    }
