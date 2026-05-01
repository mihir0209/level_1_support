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

from core.taxonomy.base import SupportCategory


# ═══════════════════════════════════════════════════════════════════════════
# ENUMS — e-commerce concrete values (kept for backward compat + seed data)
# New code should use core/taxonomy/base.py enums instead.
# ═══════════════════════════════════════════════════════════════════════════

class BrandTier(StrEnum):
    BUDGET = "budget"
    MID = "mid"
    PREMIUM = "premium"


class IssueCategory(StrEnum):
    """Legacy e-commerce categories. Deprecated — use SupportCategory from taxonomy."""
    ORDER = "order"
    RETURN = "return"
    PAYMENT = "payment"
    PRODUCT = "product"
    ACCOUNT = "account"
    PROMOTIONS = "promotions"
    DELIVERY = "delivery"
    INFO = "info"


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
    LOYALTY = "loyalty"
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
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


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
    refund_timeline_days: int = 14             # days for cash refund after return received


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
    issue_type: str                      # IssueType.id (domain-specific slug)
    confidence: float
    category: SupportCategory            # base taxonomy category
    entities: "ExtractedEntities" = Field(default_factory=dict)


class ExtractedEntities(BaseModel):
    """
    Typed entity bag extracted from the customer's message.
    Tools receive this via .model_dump(exclude_none=True) so missing fields
    are simply absent — no KeyError on tool invocation.
    """
    order_id: str | None = None
    product_sku: str | None = None
    return_reason: str | None = None
    promo_code: str | None = None
    customer_name: str | None = None
    email: str | None = None
    phone: str | None = None
    tracking_id: str | None = None
    amount_gbp: float | None = None
    size: str | None = None
    color: str | None = None
    # Catch-all for domain-specific entities not listed above
    extra: dict[str, Any] = Field(default_factory=dict)

    def model_dump(self, **kwargs) -> dict[str, Any]:
        """Flatten extra fields into the top-level dict."""
        base = super().model_dump(**kwargs)
        if self.extra:
            base.update(self.extra)
        return base


class ContextPackage(BaseModel):
    """The assembled context object passed into the LangGraph state machine."""
    brand_id: str
    customer_id: str | None
    channel: Channel

    # Assembled context blocks (in priority order)
    system_prompt: str
    user_context_block: str            # customer tier, orders, open returns
    policy_block: str                  # temporally-correct return policy text
    shipping_block: str = ""           # shipping/delivery policy (for WISMO queries)
    resolution_block: str              # resolution steps from graph
    session_block: str                 # compressed conversation history
    tool_manifest: list[str]           # tool function names available
    entities: ExtractedEntities = Field(default_factory=ExtractedEntities)

    # Metadata
    intent: IntentResult | None = None
    applicable_policy: ReturnPolicy | ShippingPolicy | None = None
    token_estimate: int = 0
    assembly_latency_ms: float = 0.0
    retrieval_confidence: float = 0.0   # confidence from KB retrieval (may exceed intent confidence)
