"""Sentiment-Based Dynamic Empathy Adapters."""

from __future__ import annotations

import json
import structlog

log = structlog.get_logger(__name__)

DEFAULT_RULES = {
    "frustrated_threshold": 0.35,
    "frustrated_mode": "friendly",
    "confused_threshold": 0.55,
    "confused_mode": "professional",
    "override_activations": 0
}


async def get_empathy_rules() -> dict:
    """Retrieve active sentiment-to-empathy-mode rules and stats."""
    from core.memory.session import get_redis
    r = get_redis()
    raw = await r.get("empathy:rules:config")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return DEFAULT_RULES


async def save_empathy_rules(config: dict) -> None:
    """Save active sentiment-to-empathy-mode rules."""
    from core.memory.session import get_redis
    r = get_redis()
    await r.set("empathy:rules:config", json.dumps(config))
    log.info("empathy_rules_saved", config=config)


async def adjust_empathy_mode(
    session_id: str,
    current_sentiment: float,
    baseline_mode: str
) -> str:
    """
    Evaluates current user sentiment and dynamically overrides the session
    empathy mode to de-escalate customer frustration.
    """
    config = await get_empathy_rules()
    frustrated_t = config.get("frustrated_threshold", 0.35)
    frustrated_m = config.get("frustrated_mode", "friendly")
    confused_t = config.get("confused_threshold", 0.55)
    confused_m = config.get("confused_mode", "professional")

    final_mode = baseline_mode

    # Sentiment ranges from 0.0 (extremely negative/frustrated) to 1.0 (positive)
    if current_sentiment <= frustrated_t:
        final_mode = frustrated_m
    elif current_sentiment <= confused_t:
        final_mode = confused_m

    if final_mode != baseline_mode:
        from core.memory.session import get_redis
        r = get_redis()
        # Increment override counts
        config["override_activations"] = config.get("override_activations", 0) + 1
        await save_empathy_rules(config)
        log.info(
            "empathy_mode_overridden_by_sentiment",
            session_id=session_id,
            sentiment=current_sentiment,
            baseline=baseline_mode,
            override=final_mode
        )

    return final_mode
