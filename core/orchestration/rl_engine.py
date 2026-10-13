"""Online Thompson-Sampling RL policy engine (Zero Cost)."""

from __future__ import annotations

import random
import structlog
from core.memory.session import get_redis

log = structlog.get_logger(__name__)

# Default arm variants for optimization categories
DEFAULT_ARMS = {
    "empathy_mode": ["professional", "friendly", "concise"],
    "compliance_strictness": ["strict", "standard"]
}


async def select_best_arm(category: str) -> str:
    """
    Select the optimal variant arm using Thompson Sampling on Beta distributions.
    Returns the selected arm name string.
    """
    arms = DEFAULT_ARMS.get(category, [])
    if not arms:
        return ""

    best_arm = arms[0]
    best_sample = -1.0

    try:
        r = get_redis()
        for arm in arms:
            alpha_key = f"rl:{category}:{arm}:alpha"
            beta_key = f"rl:{category}:{arm}:beta"
            
            raw_a = await r.get(alpha_key)
            raw_b = await r.get(beta_key)
            
            alpha = float(raw_a) if raw_a else 1.0
            beta = float(raw_b) if raw_b else 1.0
            
            # Draw sample from Beta(alpha, beta) using random.betavariate
            sample = random.betavariate(alpha, beta)
            if sample > best_sample:
                best_sample = sample
                best_arm = arm
    except Exception as e:
        log.warning("rl_select_arm_failed_fallback_default", category=category, error=str(e))
        best_arm = random.choice(arms)

    log.info("rl_arm_selected", category=category, selected=best_arm, sample_val=round(best_sample, 3))
    return best_arm


async def register_feedback(category: str, arm_name: str, reward: float) -> None:
    """
    Update Beta distribution stats based on reward feedback.
    Supports binary (1.0/0.0) and continuous rewards in range [0.0, 1.0].
    For a given reward r, we increment alpha by r and beta by 1.0 - r.
    """
    arms = DEFAULT_ARMS.get(category, [])
    if arm_name not in arms:
        return

    # Bound reward to [0.0, 1.0] range
    r_val = max(0.0, min(1.0, float(reward)))

    try:
        r = get_redis()
        alpha_key = f"rl:{category}:{arm_name}:alpha"
        beta_key = f"rl:{category}:{arm_name}:beta"

        # Initialize to 1.0 (Bayesian uniform prior baseline) if not exists
        for key in (alpha_key, beta_key):
            exists = await r.exists(key)
            if not exists:
                await r.set(key, 1.0)

        # Update alpha and beta with continuous rewards
        if r_val > 0.0:
            await r.incrbyfloat(alpha_key, r_val)
        if (1.0 - r_val) > 0.0:
            await r.incrbyfloat(beta_key, 1.0 - r_val)

        log.info("rl_feedback_registered", category=category, arm=arm_name, reward=r_val)
    except Exception as e:
        log.warning("rl_register_feedback_failed", category=category, arm=arm_name, error=str(e))


async def get_all_arm_stats(category: str) -> list[dict]:
    """Retrieve parameter stats and expectation averages for variants in a category."""
    arms = DEFAULT_ARMS.get(category, [])
    stats = []

    try:
        r = get_redis()
        for arm in arms:
            alpha_key = f"rl:{category}:{arm}:alpha"
            beta_key = f"rl:{category}:{arm}:beta"
            
            raw_a = await r.get(alpha_key)
            raw_b = await r.get(beta_key)
            
            alpha = float(raw_a) if raw_a else 1.0
            beta = float(raw_b) if raw_b else 1.0
            
            # Expected value of Beta distribution is alpha / (alpha + beta)
            expected_win_rate = alpha / (alpha + beta)
            
            stats.append({
                "arm_name": arm,
                "alpha": alpha,
                "beta": beta,
                "trials": int(alpha + beta - 2),
                "expected_win_rate": round(expected_win_rate, 3)
            })
    except Exception as e:
        log.warning("rl_get_stats_failed", category=category, error=str(e))
        
    return stats


async def reset_arm_stats(category: str) -> None:
    """Reset Alpha/Beta parameters to uniform prior Beta(1,1) for all arms."""
    arms = DEFAULT_ARMS.get(category, [])
    try:
        r = get_redis()
        for arm in arms:
            alpha_key = f"rl:{category}:{arm}:alpha"
            beta_key = f"rl:{category}:{arm}:beta"
            await r.delete(alpha_key, beta_key)
        log.info("rl_stats_reset", category=category)
    except Exception as e:
        log.warning("rl_reset_stats_failed", category=category, error=str(e))
