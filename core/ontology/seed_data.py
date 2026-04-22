"""
Boohoo Group seed data — populates the knowledge graph on first run.
Brands, policies, issue types, and resolution paths for all 13 brands.
Run via: python -m core.ontology.seed_data
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from core.ontology.models import (
    Brand, BrandTier, IssueCategory, IssueType, Resolution,
    ResolutionStep, ReturnPolicy, ShippingPolicy,
)
from core.ontology.schema import get_graph_client

log = structlog.get_logger(__name__)

# ── Effective date for initial policies ────────────────────────────────────
_POLICY_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# BRAND DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════

BRANDS: list[Brand] = [
    Brand(
        id="boohoo_uk", name="Boohoo", domain="boohoo.com",
        help_center_url="https://boohoo.com/pages/informational/FAQ",
        tier=BrandTier.BUDGET, target_demographic="Women 16-30",
        voice_persona="Friendly, helpful, casual British tone",
    ),
    Brand(
        id="boohooman_uk", name="BoohooMAN", domain="boohooman.com",
        help_center_url="https://boohooman.com/pages/informational/FAQ",
        tier=BrandTier.BUDGET, target_demographic="Men 16-30",
        voice_persona="Cool, casual, direct tone",
    ),
    Brand(
        id="plt_uk", name="PrettyLittleThing", domain="prettylittlething.com",
        help_center_url="https://prettylittlething.com/customer-service/faq",
        tier=BrandTier.BUDGET, target_demographic="Women 16-24",
        voice_persona="Bold, upbeat, gen-Z casual tone",
    ),
    Brand(
        id="nastygal_us", name="Nasty Gal", domain="nastygal.com",
        help_center_url="https://nastygal.com/pages/customer-service",
        default_locale="en-US", tier=BrandTier.MID,
        target_demographic="Women 18-35",
        voice_persona="Confident, edgy, US West Coast vibe",
    ),
    Brand(
        id="karenmillen_uk", name="Karen Millen", domain="karenmillen.com",
        help_center_url="https://karenmillen.com/pages/customer-service",
        tier=BrandTier.PREMIUM, target_demographic="Women 30-50",
        voice_persona="Warm, sophisticated, professional British tone",
    ),
    Brand(
        id="dorothyperkins_uk", name="Dorothy Perkins", domain="dorothyperkins.com",
        help_center_url="https://dorothyperkins.com/pages/help",
        tier=BrandTier.MID, target_demographic="Women 35-55",
        voice_persona="Friendly, warm, classic British tone",
    ),
    Brand(
        id="wallis_uk", name="Wallis", domain="wallis.co.uk",
        help_center_url="https://wallis.co.uk/pages/help",
        tier=BrandTier.MID, target_demographic="Women 35-60",
        voice_persona="Elegant, reliable, understated tone",
    ),
    Brand(
        id="debenhams_uk", name="Debenhams", domain="debenhams.com",
        help_center_url="https://debenhams.com/pages/help",
        tier=BrandTier.MID, target_demographic="Women/Men 25-55",
        voice_persona="Trusted, helpful, classic department store tone",
    ),
    Brand(
        id="oasis_uk", name="Oasis", domain="oasis-stores.com",
        help_center_url="https://oasis-stores.com/pages/help",
        tier=BrandTier.MID, target_demographic="Women 25-40",
        voice_persona="Feminine, stylish, friendly tone",
    ),
    Brand(
        id="warehouse_uk", name="Warehouse", domain="warehouse.co.uk",
        help_center_url="https://warehouse.co.uk/pages/help",
        tier=BrandTier.MID, target_demographic="Women 20-35",
        voice_persona="Contemporary, sharp, confident tone",
    ),
    Brand(
        id="coast_uk", name="Coast", domain="coast-stores.com",
        help_center_url="https://coast-stores.com/pages/help",
        tier=BrandTier.PREMIUM, target_demographic="Women 25-45",
        voice_persona="Elegant, occasion-focused, polished tone",
    ),
    Brand(
        id="misspap_uk", name="MissPap", domain="misspap.co.uk",
        help_center_url="https://misspap.co.uk/pages/customer-service",
        tier=BrandTier.BUDGET, target_demographic="Women 16-25",
        voice_persona="Fun, trendy, youthful tone",
    ),
    Brand(
        id="burton_uk", name="Burton", domain="burton.co.uk",
        help_center_url="https://burton.co.uk/pages/help",
        tier=BrandTier.MID, target_demographic="Men 20-40",
        voice_persona="Smart, straightforward, helpful tone",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# RETURN POLICIES (per brand, UK market baseline)
# ═══════════════════════════════════════════════════════════════════════════

RETURN_POLICIES: list[ReturnPolicy] = [
    ReturnPolicy(
        brand_id="boohoo_uk", return_window_days=28,
        return_cost_gbp=1.99, return_cost_usd=6.00,
        free_return_condition=None,
        excluded_categories=["swimwear", "lingerie", "cosmetics", "pierced jewellery"],
        effective_date=_POLICY_DATE,
    ),
    ReturnPolicy(
        brand_id="boohooman_uk", return_window_days=28,
        return_cost_gbp=1.99, return_cost_usd=6.00,
        excluded_categories=["swimwear", "underwear"],
        effective_date=_POLICY_DATE,
    ),
    ReturnPolicy(
        brand_id="plt_uk", return_window_days=28,
        return_cost_gbp=1.99,
        free_return_condition="PLT Royalty members only",
        excluded_categories=["swimwear", "lingerie", "cosmetics", "pierced jewellery"],
        effective_date=_POLICY_DATE,
    ),
    ReturnPolicy(
        brand_id="nastygal_us", return_window_days=28,
        return_cost_usd=7.00,
        excluded_categories=["swimwear", "lingerie", "cosmetics"],
        effective_date=_POLICY_DATE,
    ),
    ReturnPolicy(
        brand_id="karenmillen_uk", return_window_days=28,
        return_cost_gbp=0.0,   # Karen Millen offers free returns (premium brand)
        free_return_condition="All orders eligible for free returns",
        excluded_categories=["swimwear", "lingerie", "cosmetics"],
        effective_date=_POLICY_DATE,
    ),
    # Remaining brands get a standard mid-tier policy
    *[
        ReturnPolicy(
            brand_id=brand_id, return_window_days=28,
            return_cost_gbp=1.99,
            excluded_categories=["swimwear", "lingerie", "cosmetics"],
            effective_date=_POLICY_DATE,
        )
        for brand_id in [
            "dorothyperkins_uk", "wallis_uk", "debenhams_uk",
            "oasis_uk", "warehouse_uk", "coast_uk", "misspap_uk", "burton_uk",
        ]
    ],
]


# ═══════════════════════════════════════════════════════════════════════════
# SHIPPING POLICIES (UK standard, baseline)
# ═══════════════════════════════════════════════════════════════════════════

SHIPPING_POLICIES: list[ShippingPolicy] = [
    ShippingPolicy(
        brand_id="boohoo_uk", region="UK",
        standard_days_min=2, standard_days_max=5, standard_cost=3.99,
        express_available=True, express_days_max=1, express_cost=6.99,
        free_shipping_threshold=30.00, effective_date=_POLICY_DATE,
    ),
    ShippingPolicy(
        brand_id="boohoo_uk", region="USA",
        standard_days_min=6, standard_days_max=8, standard_cost=10.99,
        express_available=True, express_days_max=4, express_cost=14.99,
        free_shipping_threshold=100.00, effective_date=_POLICY_DATE,
    ),
    ShippingPolicy(
        brand_id="plt_uk", region="UK",
        standard_days_min=2, standard_days_max=5, standard_cost=3.99,
        express_available=True, express_days_max=1, express_cost=5.99,
        free_shipping_threshold=25.00, effective_date=_POLICY_DATE,
    ),
    ShippingPolicy(
        brand_id="nastygal_us", region="USA",
        standard_days_min=5, standard_days_max=8, standard_cost=9.95,
        express_available=True, express_days_max=3, express_cost=14.95,
        free_shipping_threshold=80.00, effective_date=_POLICY_DATE,
    ),
    ShippingPolicy(
        brand_id="karenmillen_uk", region="UK",
        standard_days_min=2, standard_days_max=4, standard_cost=0.0,  # free standard
        express_available=True, express_days_max=1, express_cost=8.00,
        free_shipping_threshold=0.0, effective_date=_POLICY_DATE,  # always free
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# ISSUE TYPES + RESOLUTIONS (universal + brand-specific)
# ═══════════════════════════════════════════════════════════════════════════

ISSUE_TYPES: list[IssueType] = [

    IssueType(
        id="wismo", name="Where Is My Order?",
        description="Customer asking about order location, tracking, or delivery status.",
        category=IssueCategory.ORDER,
        avg_resolution_time_seconds=90,
        resolutions=[
            Resolution(
                id="wismo_track",
                name="Provide tracking information",
                requires_tool_call=True,
                success_rate=0.92,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Look up order status via order_lookup tool.",
                        tool_required="order_lookup"),
                    ResolutionStep(order=2,
                        instruction="If dispatched: retrieve tracking URL via shipment_track tool "
                                    "and provide it with carrier name and ETA.",
                        tool_required="shipment_track"),
                    ResolutionStep(order=3,
                        instruction="If still processing: inform customer of typical dispatch "
                                    "timeframe from the brand's shipping policy.",
                        condition="order.status == 'processing'"),
                    ResolutionStep(order=4,
                        instruction="If estimated delivery has passed: apologise, offer to "
                                    "investigate, and ask if they want a replacement or refund.",
                        condition="order.estimated_delivery < today AND order.status != 'delivered'"),
                ],
            ),
        ],
    ),

    IssueType(
        id="return_initiate", name="How to Return an Item",
        description="Customer wants to start a return for one or more items.",
        category=IssueCategory.RETURN,
        avg_resolution_time_seconds=120,
        resolutions=[
            Resolution(
                id="return_standard",
                name="Initiate standard return",
                requires_tool_call=True,
                success_rate=0.88,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Confirm which order and which items the customer wants "
                                    "to return. Ask if not specified.",
                        tool_required=None),
                    ResolutionStep(order=2,
                        instruction="Check if items are within return window using temporal "
                                    "policy for the order's placed_at date.",
                        tool_required="policy_lookup"),
                    ResolutionStep(order=3,
                        instruction="Check if item category is excluded (swimwear, lingerie, "
                                    "cosmetics with broken seal). Inform customer if excluded.",
                        condition="item.category in excluded_categories"),
                    ResolutionStep(order=4,
                        instruction="Initiate return via return_init tool. "
                                    "Inform customer of return cost (if applicable) and refund timeline.",
                        tool_required="return_init"),
                    ResolutionStep(order=5,
                        instruction="Send returns label via email using email_label tool.",
                        tool_required="email_label"),
                ],
            ),
        ],
    ),

    IssueType(
        id="refund_delay", name="Refund Not Received",
        description="Customer chasing a refund that has not appeared yet.",
        category=IssueCategory.RETURN,
        avg_resolution_time_seconds=150,
        resolutions=[
            Resolution(
                id="refund_status_check",
                name="Check and explain refund status",
                requires_tool_call=True,
                success_rate=0.79,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Look up the return/refund status via refund_status tool.",
                        tool_required="refund_status"),
                    ResolutionStep(order=2,
                        instruction="If return received but refund not processed: explain "
                                    "standard timeline (up to 21 days: 14 days transit + 7 days bank).",
                        condition="return.received_at IS NOT NULL AND refund.processed_at IS NULL"),
                    ResolutionStep(order=3,
                        instruction="If within policy timeline (21 days): reassure customer "
                                    "and provide expected processing date.",
                        condition="days_since_return <= 21"),
                    ResolutionStep(order=4,
                        instruction="If refund is overdue (>21 days since return received): "
                                    "escalate immediately with priority P1.",
                        condition="days_since_return > 21",
                        tool_required="escalate"),
                ],
                escalation_trigger="refund_overdue_21_days",
            ),
        ],
    ),

    IssueType(
        id="wrong_item", name="Wrong Item Received",
        description="Customer received an item different from what they ordered.",
        category=IssueCategory.ORDER,
        avg_resolution_time_seconds=180,
        resolutions=[
            Resolution(
                id="wrong_item_resolution",
                name="Wrong item — replacement or refund",
                requires_tool_call=True,
                success_rate=0.85,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Sincerely apologise for the error. Ask customer to confirm "
                                    "the item received vs. item ordered (SKU or description).",
                        tool_required=None),
                    ResolutionStep(order=2,
                        instruction="Offer choice: replacement of correct item (if in stock) "
                                    "OR full refund including original shipping cost.",
                        tool_required="catalog_lookup"),
                    ResolutionStep(order=3,
                        instruction="If replacement: initiate return of wrong item (free return, "
                                    "no return fee for brand error) and dispatch correct item.",
                        tool_required="return_init"),
                    ResolutionStep(order=4,
                        instruction="If refund: initiate full refund including any delivery charge.",
                        tool_required="refund_initiate"),
                ],
            ),
        ],
    ),

    IssueType(
        id="damaged_item", name="Damaged or Faulty Item",
        description="Customer received an item that is damaged, faulty, or defective.",
        category=IssueCategory.ORDER,
        avg_resolution_time_seconds=180,
        resolutions=[
            Resolution(
                id="damaged_item_resolution",
                name="Damaged item — replacement or refund",
                requires_tool_call=True,
                success_rate=0.83,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Apologise. Confirm order ID, item, and nature of the damage/fault. "
                                    "Check claim is within 30 days of delivery.",
                        tool_required="order_lookup"),
                    ResolutionStep(order=2,
                        instruction="If claim is valid: offer replacement or full refund. "
                                    "No return fee charged for faulty items — brand covers return cost.",
                        tool_required=None),
                    ResolutionStep(order=3,
                        instruction="Process customer's chosen resolution.",
                        tool_required="return_init"),
                ],
            ),
        ],
    ),

    IssueType(
        id="order_cancel", name="Cancel an Order",
        description="Customer wants to cancel an order they just placed.",
        category=IssueCategory.ORDER,
        avg_resolution_time_seconds=60,
        resolutions=[
            Resolution(
                id="order_cancel_check",
                name="Check cancel eligibility and attempt cancellation",
                requires_tool_call=True,
                success_rate=0.55,   # low because orders process very quickly
                steps=[
                    ResolutionStep(order=1,
                        instruction="Look up order status immediately via order_lookup.",
                        tool_required="order_lookup"),
                    ResolutionStep(order=2,
                        instruction="If status is 'processing': attempt cancellation via "
                                    "cancel_order tool. Warehouse picks orders within 1 hour.",
                        tool_required="cancel_order",
                        condition="order.status == 'processing'"),
                    ResolutionStep(order=3,
                        instruction="If status is 'dispatched' or later: cancellation is no "
                                    "longer possible. Advise customer to refuse delivery OR "
                                    "return item when it arrives. Explain return process.",
                        condition="order.status in ['dispatched', 'in_transit', 'delivered']"),
                ],
            ),
        ],
    ),

    IssueType(
        id="payment_failed", name="Payment Declined",
        description="Customer's payment was declined or not processed correctly.",
        category=IssueCategory.PAYMENT,
        avg_resolution_time_seconds=120,
        resolutions=[
            Resolution(
                id="payment_troubleshoot",
                name="Troubleshoot payment failure",
                requires_tool_call=False,
                success_rate=0.70,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Ask which payment method was used and what error message appeared."),
                    ResolutionStep(order=2,
                        instruction="For card payments: suggest checking card details, billing address, "
                                    "3DS verification, and sufficient funds. Suggest trying another card."),
                    ResolutionStep(order=3,
                        instruction="For PayPal: suggest logging out and back in, or using "
                                    "PayPal guest checkout with card directly."),
                    ResolutionStep(order=4,
                        instruction="For Klarna/Afterpay: suggest checking their app for account "
                                    "status; BNPL platforms may have spending limit restrictions.",
                        condition="payment_method in ['klarna', 'afterpay', 'clearpay']"),
                    ResolutionStep(order=5,
                        instruction="If issue persists: direct to payment provider's support. "
                                    "Note: we do not store card details — customer must re-enter.",
                        tool_required=None),
                ],
            ),
        ],
    ),

    IssueType(
        id="promo_not_applied", name="Discount Code Not Working",
        description="Customer's promotional or discount code is not applying at checkout.",
        category=IssueCategory.PROMOTIONS,
        avg_resolution_time_seconds=90,
        resolutions=[
            Resolution(
                id="promo_validate",
                name="Validate and troubleshoot promotional code",
                requires_tool_call=True,
                success_rate=0.75,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Ask for the exact code and which brand/site they're shopping on.",
                        tool_required=None),
                    ResolutionStep(order=2,
                        instruction="Validate the code via promo_validate tool — check if code "
                                    "is active, not expired, and applies to this brand.",
                        tool_required="promo_validate"),
                    ResolutionStep(order=3,
                        instruction="Check common exclusions: sale items, already-discounted items, "
                                    "minimum spend not met, code already used, code for different brand.",
                        tool_required=None),
                    ResolutionStep(order=4,
                        instruction="If code is valid but not working: advise clearing browser cache "
                                    "or trying a different browser/device."),
                    ResolutionStep(order=5,
                        instruction="If code should work but doesn't: escalate to raise a manual "
                                    "adjustment request.",
                        tool_required="escalate"),
                ],
            ),
        ],
    ),

    IssueType(
        id="klarna_return", name="Klarna Return & Refund",
        description="Customer using Klarna BNPL asking about return/refund flow.",
        category=IssueCategory.RETURN,
        avg_resolution_time_seconds=150,
        resolutions=[
            Resolution(
                id="klarna_return_flow",
                name="Explain Klarna return flow",
                requires_tool_call=True,
                success_rate=0.80,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Confirm return eligibility as per standard return policy.",
                        tool_required="policy_lookup"),
                    ResolutionStep(order=2,
                        instruction="Explain Klarna-specific flow: initiate return normally — "
                                    "Klarna payments are paused while return is in transit. "
                                    "Klarna will update their app once refund is processed."),
                    ResolutionStep(order=3,
                        instruction="IMPORTANT: Advise customer NOT to continue Klarna payments "
                                    "while the return is in progress — Klarna will adjust the balance."),
                    ResolutionStep(order=4,
                        instruction="Initiate return if not already done.",
                        tool_required="return_init"),
                ],
            ),
        ],
    ),

    IssueType(
        id="size_query", name="Size Guide Query",
        description="Customer asking about sizing, fit, or which size to order.",
        category=IssueCategory.PRODUCT,
        avg_resolution_time_seconds=60,
        resolutions=[
            Resolution(
                id="size_guide_lookup",
                name="Provide size guide information",
                requires_tool_call=True,
                success_rate=0.88,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Identify the product category (dresses, tops, jeans, footwear) "
                                    "and customer's usual UK/US/EU size.",
                        tool_required=None),
                    ResolutionStep(order=2,
                        instruction="Retrieve size guide via size_guide tool for the product category.",
                        tool_required="size_guide"),
                    ResolutionStep(order=3,
                        instruction="Advise: if between sizes, recommend sizing up for comfort. "
                                    "Remind customer of the return policy if the size doesn't work."),
                ],
            ),
        ],
    ),

    IssueType(
        id="account_locked", name="Cannot Log In / Account Issues",
        description="Customer cannot access their account (forgotten password, locked account).",
        category=IssueCategory.ACCOUNT,
        avg_resolution_time_seconds=90,
        resolutions=[
            Resolution(
                id="account_reset",
                name="Account access troubleshooting",
                requires_tool_call=True,
                success_rate=0.90,
                steps=[
                    ResolutionStep(order=1,
                        instruction="Ask if they have forgotten their password or if the account "
                                    "appears locked/suspended."),
                    ResolutionStep(order=2,
                        instruction="For forgotten password: direct to 'Forgot Password' link on "
                                    "the brand's login page — sends reset email instantly.",
                        tool_required=None),
                    ResolutionStep(order=3,
                        instruction="If reset email not received: advise checking spam/junk folder "
                                    "and ensuring correct email is used.",
                        tool_required=None),
                    ResolutionStep(order=4,
                        instruction="If account appears suspended: verify identity and check "
                                    "account status via account_status tool.",
                        tool_required="account_status"),
                    ResolutionStep(order=5,
                        instruction="If suspension confirmed and customer disputes it: escalate "
                                    "to account review team.",
                        tool_required="escalate"),
                ],
            ),
        ],
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# GRAPH POPULATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def upsert_brand(graph, brand: Brand) -> None:
    graph.query(
        """
        MERGE (b:Brand {id: $id})
        SET b.name = $name,
            b.group_id = $group_id,
            b.domain = $domain,
            b.help_center_url = $help_center_url,
            b.default_locale = $default_locale,
            b.tier = $tier,
            b.target_demographic = $target_demographic,
            b.voice_persona = $voice_persona,
            b.chatwoot_inbox_id = $chatwoot_inbox_id
        """,
        {
            "id": brand.id, "name": brand.name, "group_id": brand.group_id,
            "domain": brand.domain, "help_center_url": brand.help_center_url,
            "default_locale": brand.default_locale, "tier": brand.tier.value,
            "target_demographic": brand.target_demographic,
            "voice_persona": brand.voice_persona,
            "chatwoot_inbox_id": brand.chatwoot_inbox_id,
        },
    )


def upsert_return_policy(graph, policy: ReturnPolicy) -> None:
    graph.query(
        """
        MERGE (p:ReturnPolicy {id: $id})
        SET p.brand_id = $brand_id,
            p.return_window_days = $return_window_days,
            p.return_cost_gbp = $return_cost_gbp,
            p.return_cost_usd = $return_cost_usd,
            p.free_return_condition = $free_return_condition,
            p.excluded_categories = $excluded_categories,
            p.effective_date = $effective_date,
            p.expiry_date = $expiry_date
        WITH p
        MATCH (b:Brand {id: $brand_id})
        MERGE (b)-[:HAS_POLICY {type: 'return', effectiveDate: $effective_date}]->(p)
        """,
        {
            "id": policy.id, "brand_id": policy.brand_id,
            "return_window_days": policy.return_window_days,
            "return_cost_gbp": policy.return_cost_gbp,
            "return_cost_usd": policy.return_cost_usd,
            "free_return_condition": policy.free_return_condition,
            "excluded_categories": policy.excluded_categories,
            "effective_date": policy.effective_date.isoformat(),
            "expiry_date": policy.expiry_date.isoformat() if policy.expiry_date else None,
        },
    )


def upsert_shipping_policy(graph, policy: ShippingPolicy) -> None:
    graph.query(
        """
        MERGE (p:ShippingPolicy {id: $id})
        SET p.brand_id = $brand_id,
            p.region = $region,
            p.standard_days_min = $standard_days_min,
            p.standard_days_max = $standard_days_max,
            p.standard_cost = $standard_cost,
            p.express_available = $express_available,
            p.express_days_max = $express_days_max,
            p.express_cost = $express_cost,
            p.free_shipping_threshold = $free_shipping_threshold,
            p.effective_date = $effective_date
        WITH p
        MATCH (b:Brand {id: $brand_id})
        MERGE (b)-[:HAS_POLICY {type: 'shipping', region: $region, effectiveDate: $effective_date}]->(p)
        """,
        {
            "id": policy.id, "brand_id": policy.brand_id, "region": policy.region,
            "standard_days_min": policy.standard_days_min,
            "standard_days_max": policy.standard_days_max,
            "standard_cost": policy.standard_cost,
            "express_available": policy.express_available,
            "express_days_max": policy.express_days_max,
            "express_cost": policy.express_cost,
            "free_shipping_threshold": policy.free_shipping_threshold,
            "effective_date": policy.effective_date.isoformat(),
        },
    )


def upsert_issue_type(graph, issue: IssueType) -> None:
    graph.query(
        """
        MERGE (it:IssueType {id: $id})
        SET it.name = $name,
            it.description = $description,
            it.category = $category,
            it.brand_id = $brand_id,
            it.avg_resolution_time_seconds = $avg_resolution_time_seconds
        """,
        {
            "id": issue.id, "name": issue.name, "description": issue.description,
            "category": issue.category.value, "brand_id": issue.brand_id,
            "avg_resolution_time_seconds": issue.avg_resolution_time_seconds,
        },
    )
    for resolution in issue.resolutions:
        steps_data = [
            {"order": s.order, "instruction": s.instruction,
             "tool_required": s.tool_required, "condition": s.condition}
            for s in resolution.steps
        ]
        graph.query(
            """
            MERGE (r:Resolution {id: $res_id})
            SET r.name = $name,
                r.requires_tool_call = $requires_tool_call,
                r.success_rate = $success_rate,
                r.sample_size = $sample_size,
                r.avg_satisfaction_score = $avg_satisfaction_score,
                r.steps = $steps,
                r.escalation_trigger = $escalation_trigger
            WITH r
            MATCH (it:IssueType {id: $issue_id})
            MERGE (it)-[:RESOLVED_BY {priority: 1}]->(r)
            """,
            {
                "res_id": resolution.id, "name": resolution.name,
                "requires_tool_call": resolution.requires_tool_call,
                "success_rate": resolution.success_rate,
                "sample_size": resolution.sample_size,
                "avg_satisfaction_score": resolution.avg_satisfaction_score,
                "steps": str(steps_data),
                "escalation_trigger": resolution.escalation_trigger,
                "issue_id": issue.id,
            },
        )


def seed_customers(graph) -> None:
    """Seed demo customers with CLIENT_OF relationship to their brand."""
    from core.tools.registry import _MOCK_CUSTOMERS
    for c in _MOCK_CUSTOMERS.values():
        cid = c["customer_id"]
        brand_id = c["brand_id"]
        name = c["name"].replace("'", "")
        email = c["email"]
        tier = c["tier"]
        locale = c["locale"]
        total_orders = c["total_orders"]
        graph.query(
            f"MERGE (c:Customer {{customer_id: '{cid}'}}) "
            f"SET c.brand_id = '{brand_id}', c.name = '{name}', "
            f"    c.email = '{email}', c.tier = '{tier}', "
            f"    c.locale = '{locale}', c.total_orders = {total_orders} "
            f"WITH c MATCH (b:Brand {{brand_id: '{brand_id}'}}) "
            f"MERGE (c)-[:CLIENT_OF]->(b)"
        )


def seed_all(graph) -> None:
    """Run full seed. Idempotent — safe to re-run."""
    log.info("seeding_brands", count=len(BRANDS))
    for brand in BRANDS:
        upsert_brand(graph, brand)

    log.info("seeding_return_policies", count=len(RETURN_POLICIES))
    for policy in RETURN_POLICIES:
        upsert_return_policy(graph, policy)

    log.info("seeding_shipping_policies", count=len(SHIPPING_POLICIES))
    for policy in SHIPPING_POLICIES:
        upsert_shipping_policy(graph, policy)

    log.info("seeding_issue_types", count=len(ISSUE_TYPES))
    for issue in ISSUE_TYPES:
        upsert_issue_type(graph, issue)

    log.info("seeding_demo_customers")
    try:
        seed_customers(graph)
    except Exception as e:
        log.warning("customer_seed_failed", error=str(e))

    log.info("seed_complete")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    _, graph = get_graph_client()
    from core.ontology.schema import create_schema
    create_schema(graph)
    seed_all(graph)
    print("✓ Boohoo Group knowledge graph seeded successfully.")
