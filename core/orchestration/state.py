"""
LangGraph state schema for the L1 Support agent.
One state instance = one support session (one customer conversation).
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from core.ontology.models import Channel, ContextPackage, Customer, IntentResult, SessionOutcome


class L1SupportState(BaseModel):
    """
    Full state for one L1 support session.
    Persisted across turns within a session.
    LangGraph serialises this between node invocations.
    """

    # ── Immutable session identifiers ──────────────────────────────────
    session_id: str
    brand_id: str
    channel: Channel
    customer_id: str | None = None

    # ── Conversation ───────────────────────────────────────────────────
    # LangGraph's add_messages annotation handles append-only accumulation.
    # Typed as BaseMessage list so HumanMessage/AIMessage objects coerce cleanly.
    messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)
    turn_count: int = 0

    # ── Intent & Entities (updated each turn) ─────────────────────────
    intent: IntentResult | None = None
    entities: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0

    # ── Context (assembled each turn by Context Engine) ────────────────
    context_package: ContextPackage | None = None
    customer: Customer | None = None

    # ── Resolution tracking ────────────────────────────────────────────
    resolution_attempts: int = 0
    current_resolution_id: str | None = None
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    last_tool_called: str | None = None

    # ── Specialist agent routing ───────────────────────────────────────
    routed_to_agent: str | None = None   # which specialist handled this turn
    specialist_response: str | None = None  # output from specialist agent (already includes tools)
    specialist_used: bool = False          # whether a specialist ran this turn

    # ── Outcome ────────────────────────────────────────────────────────
    is_resolved: bool = False
    escalation_reason: str | None = None
    outcome: SessionOutcome | None = None
    satisfaction: float | None = None   # 0.0–1.0 CSAT score

    class Config:
        arbitrary_types_allowed = True
