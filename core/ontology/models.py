"""
Domain model definitions — the formal ontology in Python/Pydantic.
These are the canonical types used everywhere in the system.
Graph nodes are created from these models.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════════════════

class BrandTier(StrEnum):
    BUDGET = "budget"
    MID = "mid"
    PREMIUM = "premium"


class IssueCategory(StrEnum):
    ORDER = "order"
    RETURN = "return"
    PAYMENT = "payment"
    PRODUCT = "product"
    ACCOUNT = "account"
    PROMOTIONS = "promotions"
    DELIVERY = "delivery"


class OrderStatus(StrEnum):
    PROCESSING = "processing"
    DISPATCHED = "dispatched"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    RETURN_PENDING = "return_pending"
    RETURNED = "returned"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CustomerTier(StrEnum):
    STANDARD = "standard"
    LOYALTY = "loyalty"        # e.g. PLT Royalty, Boohoo Premier
    VIP = "vip"


class Channel(StrEnum):
    CHAT = "chat"
    EMAIL = "email"
    VOICE = "voice"
    WHATSAPP = "whatsapp"


class SessionOutcome(StrEnum):
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"


class EscalationTier(StrEnum):
    L1 = "L1"     # other AI agents
    L2 = "L2"     # human L1 agent
    L3 = "L3"     # specialist / manager


# ═══════════════════════════════════════════════════════════════════════════
# BRAND DOMAIN
# ═══════════════════════════════════════════════════════════════════════════

class Brand(BaseModel):
    id: str                          # e.g. "boohoo_uk", "plt_uk"
    name: str                        # "Boohoo", "PrettyLittleThing"
    group_id: str = "boohoo_group"
    domain: str                      # "boohoo.com"
    help_center_url: str
    default_locale: str = "en-GB"
    tier: BrandTier
    target_demographic: str
    voice_persona: str               # for voice channel TTS persona
    chatwoot_inbox_id: int | None = None


class ReturnPolicy(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    brand_id: str
    return_window_days: int
    return_cost_gbp: float = 0.0
    return_cost_usd: float = 0.0
    free_return_condition: str | None = None   # e.g. "PLT Royalty members only"
    excluded_categories: list[str] = Field(default_factory=list)
    effective_date: datetime
    expiry_date: datetime | None = None        # None = still active


class ShippingPolicy(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    brand_id: str
    region: str                     # "UK", "USA", "Canada"
    standard_days_min: int
    standard_days_max: int
    standard_cost: float
    express_available: bool = False
    express_days_max: int | None = None
    express_cost: float | None = None
    free_shipping_threshold: float | None = None
    effective_date: datetime
    expiry_date: datetime | None = None


# ═══════════════════════════════════════════════════════════════════════════
# ISSUE DOMAIN
# ═══════════════════════════════════════════════════════════════════════════

class ResolutionStep(BaseModel):
    order: int
    instruction: str
    tool_required: str | None = None    # tool function name if action needed
    condition: str | None = None        # when does this step apply


class Resolution(BaseModel):
    id: str
    name: str
    steps: list[ResolutionStep]
    requires_tool_call: bool = False
    success_rate: float = 0.0           # updated by feedback loop
    sample_size: int = 0
    avg_satisfaction_score: float = 0.0
    last_validated_at: datetime | None = None
    escalation_trigger: str | None = None


class IssueType(BaseModel):
    id: str                             # canonical slug e.g. "wismo"
    name: str
    description: str
    category: IssueCategory
    brand_id: str | None = None         # None = universal across all brands
    avg_resolution_time_seconds: int = 180
    resolutions: list[Resolution] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMER DOMAIN
# ═══════════════════════════════════════════════════════════════════════════

class OrderItem(BaseModel):
    sku: str
    name: str
    quantity: int
    price_paid: float
    size: str | None = None
    color: str | None = None


class Shipment(BaseModel):
    tracking_id: str | None = None
    carrier: str | None = None
    estimated_delivery: datetime | None = None
    current_status: str | None = None
    tracking_url: str | None = None


class Order(BaseModel):
    id: str
    brand_id: str
    customer_id: str
    status: OrderStatus
    placed_at: datetime                  # CRITICAL for temporal policy lookup
    items: list[OrderItem] = Field(default_factory=list)
    shipment: Shipment | None = None
    total_value: float = 0.0
    currency: str = "GBP"


class Customer(BaseModel):
    id: str
    brand_id: str
    email: str | None = None
    tier: CustomerTier = CustomerTier.STANDARD
    locale: str = "en-GB"
    total_orders: int = 0
    recent_orders: list[Order] = Field(default_factory=list)
    open_returns: list[dict[str, Any]] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# CONTEXT ENGINEERING MODELS
# ═══════════════════════════════════════════════════════════════════════════

class IntentResult(BaseModel):
    issue_type: str                      # IssueType.id
    confidence: float
    category: IssueCategory
    entities: dict[str, Any] = Field(default_factory=dict)
    # entities: {order_id, product_sku, return_reason, promo_code, ...}


class ContextPackage(BaseModel):
    """The assembled context object passed into the LangGraph state machine."""
    brand_id: str
    customer_id: str | None
    channel: Channel

    # Assembled context blocks (in priority order)
    system_prompt: str
    user_context_block: str            # customer tier, orders, open returns
    policy_block: str                  # temporally-correct policy text
    resolution_block: str              # resolution steps from graph
    session_block: str                 # compressed conversation history
    tool_manifest: list[str]           # tool function names available
    entities: dict[str, Any]

    # Metadata
    intent: IntentResult | None = None
    applicable_policy: ReturnPolicy | ShippingPolicy | None = None
    token_estimate: int = 0
    assembly_latency_ms: float = 0.0
