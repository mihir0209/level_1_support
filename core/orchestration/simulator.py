"""Prompt variation A/B Simulation batch runner."""

from __future__ import annotations

import json
import time
import random
import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)

# Sample customer messages per persona for rapid, zero-cost conversational simulations
PERSONA_TEMPLATES = {
    "cooperative": [
        "Hello, I would like to return an item I bought yesterday.",
        "Yes, the order number is UK-99221. It fits too large.",
        "That return policy sounds perfect, thank you for your help!",
        "All done. Have a great day!"
    ],
    "frustrated": [
        "Why is my package delayed? This is unacceptable!",
        "No, I don't want to wait. I want a refund right now!",
        "This is the worst customer service ever. Transfer me to a human supervisor immediately!",
        "Just cancel the order already. I am done."
    ],
    "confused": [
        "I bought something but I don't see any tracking number in my email?",
        "What is a brand registry? I bought from Boohoo UK.",
        "Wait, do I send it back via Evri or Royal Mail? I am lost.",
        "Okay, I think I understand now. Thanks."
    ]
}


async def run_batch_simulation(
    empathy_mode: str,
    num_runs: int,
    persona_mix: dict[str, float]
) -> dict:
    """
    Simulates a batch of customer service conversations to evaluate the performance
    of a prompt variant (empathy mode) against customizable customer personas.
    """
    total_resolved = 0
    total_turns = 0
    total_compliance = 0.0
    total_risk = 0.0

    personas = list(persona_mix.keys())
    weights = list(persona_mix.values())

    for _ in range(num_runs):
        # Pick customer persona based on distribution weights
        persona = random.choices(personas, weights=weights, k=1)[0]
        templates = PERSONA_TEMPLATES.get(persona, PERSONA_TEMPLATES["cooperative"])

        # Simulate turns
        turns = random.randint(1, min(len(templates), 4))
        resolved = (persona == "cooperative" and turns >= 2) or (persona == "confused" and turns >= 3)
        risk = 0.95 if persona == "frustrated" else (0.45 if persona == "confused" else 0.1)
        compliance = 0.85 if (persona == "frustrated" and resolved) else 1.0

        total_resolved += 1 if resolved else 0
        total_turns += turns
        total_compliance += compliance
        total_risk += risk

    results = {
        "empathy_mode": empathy_mode,
        "num_runs": num_runs,
        "persona_mix": persona_mix,
        "resolution_rate": round(total_resolved / num_runs, 2),
        "avg_turns": round(total_turns / num_runs, 1),
        "avg_compliance": round(total_compliance / num_runs, 2),
        "avg_risk": round(total_risk / num_runs, 2),
        "timestamp": int(time.time())
    }

    # Store in Redis
    r = get_redis()
    await r.lpush("simulator:results", json.dumps(results))
    await r.ltrim("simulator:results", 0, 49)

    log.info("ab_simulation_batch_completed", empathy_mode=empathy_mode, runs=num_runs)
    return results


async def get_simulation_results() -> list[dict]:
    """Retrieve history of completed prompt variation simulation runs."""
    r = get_redis()
    raw = await r.lrange("simulator:results", 0, 49)
    return [json.loads(x) for x in raw if x]
