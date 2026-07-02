"""Multi-Agent task decomposition and coordinator dispatch."""

from __future__ import annotations

import json
import structlog
from core.orchestration.llm import get_llm_client

log = structlog.get_logger(__name__)


async def decompose_compound_request(message: str) -> list[dict]:
    """
    Decompose a potentially compound user query into specialized sub-tasks.
    Each task gets assigned a type (returns | shipping_wismo | standard) and text.
    """
    prompt = f"""
    Analyze the following customer support request. Check if it is a compound query containing multiple distinct requests (e.g., tracking an order AND returning an item).

    Request: "{message}"

    Decompose this request into a JSON list of sub-tasks. Each sub-task must contain:
    - "task_type": "returns" | "shipping_wismo" | "standard"
    - "sub_query": The specific sentence or segment for this task.
    - "explanation": Why this task was separated.

    If the query is simple and contains only a single request, return a single task in the list.
    Respond with ONLY the raw JSON list of sub-tasks. Do not include markdown code block syntax or explanations.

    JSON Format Example:
    [
      {{
        "task_type": "shipping_wismo",
        "sub_query": "Where is my first order 1001?",
        "explanation": "Customer is requesting tracking information."
      }},
      {{
        "task_type": "returns",
        "sub_query": "I want to return item SKU-99 from my second order 1002.",
        "explanation": "Customer wants to return an item."
      }}
    ]
    """

    llm = get_llm_client()
    try:
        res = await llm.generate(
            prompt=prompt,
            system="You are a precise task decomposer. Respond with ONLY a valid JSON list.",
            fast=True,
            max_tokens=512,
        )
        clean_res = res.strip()
        if clean_res.startswith("```"):
            lines = clean_res.split("\n")
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                clean_res = "\n".join(lines[1:-1]).strip()
                
        try:
            tasks = json.loads(clean_res)
            if isinstance(tasks, list):
                return tasks
        except json.JSONDecodeError as je:
            healed = await llm.self_heal_json(clean_res, str(je))
            if isinstance(healed, list):
                return healed
    except Exception as e:
        log.warning("query_decomposition_failed", error=str(e))
        
    return [{
        "task_type": "standard",
        "sub_query": message,
        "explanation": "Fallback single task mapping."
    }]
