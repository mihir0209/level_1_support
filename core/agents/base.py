"""
Re-exports from core.agents.simple for backward compatibility.

The actual agent implementation lives in core.agents.simple (no swarms dependency).
"""

from core.agents.simple import (
    build_agent,
    run_specialist,
    SimpleAgent,
)

__all__ = ["build_agent", "run_specialist", "SimpleAgent"]
