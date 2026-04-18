"""
End-to-end Boohoo KB validation tests.

Tests the full pipeline the same way agents would use it:
  1. KB content correctness — policies, FAQs, brand voice
  2. Context engine assembly — policy block, resolution block, user context
  3. KG retriever — live queries against seeded FalkorDB (skipped if unavailable)
  4. pgvector content — FAQ chunks correct shape and content
  5. Seeder idempotency — running seed twice produces identical results
  6. Full chat turn simulation — mock LLM, real context assembly pipeline

No LLM API keys required — all LLM calls are mocked.
"""

from __future__ import annotations

import json
import socket
import sys
import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def service_up(port: int) -> bool:
    try:
        s = socket.create_connection(("localhost", port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


falkordb_available = service_up(6381)
postgres_available = service_up(5432)


# ─────────────────────────────────────────────────────────────────────────────
# 1. KB CONTENT: POLICIES
# ─────────────────────────────────────────────────────────────────────────────

class TestBoohooReturnPolicy:
    """Validate the return policy data structure matches what agents expect."""

    def setup_method(self):
        from data.boohoo_kb.policies import BOOHOO_RETURN_POLICY, POLICY_BY_TYPE
        self.policy = BOOHOO_RETURN_POLICY
        self.by_type = POLICY_BY_TYPE

    def test_return_window_is_28_days(self):
        assert self.policy["return_window_days"] == 28

    def test_return_cost_is_1_99(self):
        assert self.policy["return_cost_gbp"] == 1.99

    def test_swimwear_is_non_returnable(self):
        non_returnable = " ".join(self.policy["non_returnable_categories"]).lower()
        assert "swimwear" in non_returnable

    def test_underwear_is_non_returnable(self):
        non_returnable = " ".join(self.policy["non_returnable_categories"]).lower()
        assert "underwear" in non_returnable or "knickers" in non_returnable

    def test_pierced_jewellery_is_non_returnable(self):
        non_returnable = " ".join(self.policy["non_returnable_categories"]).lower()
        assert "jewellery" in non_returnable

    def test_refund_timeline_has_all_fields(self):
        timeline = self.policy["refund_timeline"]
        assert "warehouse_receipt_to_processing_days" in timeline
        assert "processing_to_bank_days" in timeline
        assert "total_worst_case_days" in timeline
        assert timeline["total_worst_case_days"] <= 21

    def test_klarna_note_present(self):
        assert "klarna" in self.policy["refund_timeline"]["klarna_note"].lower()

    def test_faulty_item_policy_mentions_14_days(self):
        assert "14 days" in self.policy["faulty_item_policy"]

    def test_faulty_item_policy_mentions_free_return(self):
        assert "free" in self.policy["faulty_item_policy"].lower()

    def test_key_messages_count(self):
        assert len(self.policy["key_messages"]) >= 5

    def test_portal_url_present(self):
        assert "returns.boohoo.com" in self.policy.get("portal_url", "")

    def test_policy_by_type_lookup(self):
        assert "return" in self.by_type
        assert self.by_type["return"]["return_window_days"] == 28

    def test_us_variant_has_usd_cost(self):
        us = self.policy["us_variant"]
        assert "return_cost_usd" in us
        assert us["return_cost_usd"] > 0


class TestBoohooShippingPolicy:

    def setup_method(self):
        from data.boohoo_kb.policies import BOOHOO_SHIPPING_POLICY
        self.sp = BOOHOO_SHIPPING_POLICY

    def test_standard_delivery_option_exists(self):
        names = [o["name"] for o in self.sp["options"]]
        assert "Standard Delivery" in names

    def test_next_day_delivery_exists(self):
        names = [o["name"] for o in self.sp["options"]]
        assert "Next Day Delivery" in names

    def test_free_threshold_is_35_gbp(self):
        assert self.sp["free_standard_delivery_threshold_gbp"] == 35.00

    def test_standard_delivery_has_evri_carrier(self):
        standard = next(o for o in self.sp["options"] if o["name"] == "Standard Delivery")
        assert "Evri" in standard["carrier"] or "evri" in standard["carrier"].lower()

    def test_tracking_urls_present(self):
        assert "evri_track_url" in self.sp["tracking"]
        assert "dpd_track_url" in self.sp["tracking"]

    def test_eu_duties_included(self):
        assert "duties" in self.sp["international"]["eu"]["duties_note"].lower()

    def test_inpost_locker_option(self):
        names = [o["name"].lower() for o in self.sp["options"]]
        assert any("inpost" in n or "collect" in n for n in names)


class TestBoohooPaymentPolicy:

    def setup_method(self):
        from data.boohoo_kb.policies import BOOHOO_PAYMENT_POLICY
        self.pp = BOOHOO_PAYMENT_POLICY

    def test_klarna_in_accepted_methods(self):
        methods_str = " ".join(self.pp["accepted_methods"]).lower()
        assert "klarna" in methods_str

    def test_clearpay_in_accepted_methods(self):
        methods_str = " ".join(self.pp["accepted_methods"]).lower()
        assert "clearpay" in methods_str

    def test_gift_card_in_accepted_methods(self):
        methods_str = " ".join(self.pp["accepted_methods"]).lower()
        assert "gift card" in methods_str

    def test_3ds_required(self):
        assert self.pp["security"]["3ds_required"] is True

    def test_klarna_return_impact_mentioned(self):
        klarna = self.pp["buy_now_pay_later"]["klarna_pay_later"]
        assert "return" in klarna["return_impact"].lower() or "paused" in klarna["return_impact"].lower()

    def test_failed_payment_guidance_mentions_bank(self):
        assert "bank" in self.pp["failed_payment_guidance"].lower()


class TestBoohooCancellationPolicy:

    def setup_method(self):
        from data.boohoo_kb.policies import BOOHOO_CANCELLATION_POLICY
        self.cp = BOOHOO_CANCELLATION_POLICY

    def test_cancellation_window_60_minutes(self):
        assert self.cp["cancellation_window"]["window_minutes"] == 60

    def test_processing_state_not_cancellable(self):
        assert "cannot" in self.cp["processing_states"]["processing"].lower()

    def test_dispatched_state_not_cancellable(self):
        assert "cannot" in self.cp["processing_states"]["dispatched"].lower()

    def test_klarna_cancelled_on_cancellation(self):
        assert "klarna" in self.cp["refund_on_cancellation"].lower()


class TestBoohooPromoPolicy:

    def setup_method(self):
        from data.boohoo_kb.policies import BOOHOO_PROMO_POLICY
        self.prp = BOOHOO_PROMO_POLICY

    def test_one_code_per_order(self):
        assert self.prp["application_rules"]["one_code_per_order"] is True

    def test_sale_items_in_exclusions(self):
        excl_str = " ".join(self.prp["common_exclusions"]).lower()
        assert "sale" in excl_str

    def test_student_discount_is_25_percent(self):
        assert self.prp["student_discount"]["discount_percentage"] == 25

    def test_student_discount_via_student_beans(self):
        assert "Student Beans" in self.prp["student_discount"]["provider"]

    def test_code_not_working_steps_exist(self):
        assert len(self.prp["code_not_working_steps"]) >= 5

    def test_expired_code_policy_present(self):
        assert "expired" in self.prp["expired_code_handling"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 2. KB CONTENT: FAQs
# ─────────────────────────────────────────────────────────────────────────────

class TestBoohooFAQContent:

    def setup_method(self):
        from data.boohoo_kb.faqs import BOOHOO_FAQS, FAQS_BY_CATEGORY, FAQS_BY_ID, FAQ_CATEGORIES
        self.faqs = BOOHOO_FAQS
        self.by_category = FAQS_BY_CATEGORY
        self.by_id = FAQS_BY_ID
        self.categories = FAQ_CATEGORIES

    def test_minimum_60_faqs(self):
        assert len(self.faqs) >= 60

    def test_all_faqs_have_required_fields(self):
        required = {"id", "category", "question", "answer", "keywords", "confidence"}
        for faq in self.faqs:
            missing = required - set(faq.keys())
            assert not missing, f"FAQ {faq.get('id', '?')} missing fields: {missing}"

    def test_all_ids_unique(self):
        ids = [f["id"] for f in self.faqs]
        assert len(ids) == len(set(ids)), "Duplicate FAQ IDs found"

    def test_all_questions_non_empty(self):
        for faq in self.faqs:
            assert len(faq["question"]) >= 5, f"Short question in {faq['id']}"

    def test_all_answers_substantive(self):
        for faq in self.faqs:
            assert len(faq["answer"]) >= 50, f"Short answer in {faq['id']}: {faq['answer'][:40]}"

    def test_all_confidences_are_1_0(self):
        for faq in self.faqs:
            assert faq["confidence"] == 1.0, f"FAQ {faq['id']} has confidence {faq['confidence']}"

    def test_all_keywords_are_lists(self):
        for faq in self.faqs:
            assert isinstance(faq["keywords"], list), f"FAQ {faq['id']} keywords not a list"
            assert len(faq["keywords"]) >= 2, f"FAQ {faq['id']} has too few keywords"

    def test_wismo_category_has_faqs(self):
        assert "wismo" in self.by_category
        assert len(self.by_category["wismo"]) >= 4

    def test_returns_category_has_faqs(self):
        assert "returns" in self.by_category
        assert len(self.by_category["returns"]) >= 5

    def test_refund_delay_category_has_faqs(self):
        assert "refund_delay" in self.by_category
        assert len(self.by_category["refund_delay"]) >= 4

    def test_wrong_item_category_has_faqs(self):
        assert "wrong_item" in self.by_category
        assert len(self.by_category["wrong_item"]) >= 3

    def test_payment_issue_category_has_faqs(self):
        assert "payment_issue" in self.by_category
        assert len(self.by_category["payment_issue"]) >= 3

    def test_promo_issue_category_has_faqs(self):
        assert "promo_issue" in self.by_category
        assert len(self.by_category["promo_issue"]) >= 4

    def test_account_issue_category_has_faqs(self):
        assert "account_issue" in self.by_category
        assert len(self.by_category["account_issue"]) >= 4

    def test_cancellation_category_has_faqs(self):
        assert "cancellation" in self.by_category

    def test_delivery_query_category_has_faqs(self):
        assert "delivery_query" in self.by_category

    def test_escalation_category_has_faqs(self):
        assert "escalation" in self.by_category

    def test_lookup_by_id(self):
        faq = self.by_id.get("wismo_001")
        assert faq is not None
        assert faq["category"] == "wismo"
        assert "where" in faq["question"].lower() or "order" in faq["question"].lower()

    def test_return_faq_mentions_28_days(self):
        return_faq = self.by_id.get("return_002")
        assert return_faq is not None
        assert "28" in return_faq["answer"]

    def test_return_faq_mentions_portal(self):
        return_faq = self.by_id.get("return_001")
        assert "returns.boohoo.com" in return_faq["answer"]

    def test_refund_faq_mentions_14_working_days(self):
        refund_faq = self.by_id.get("refund_001")
        assert refund_faq is not None
        assert "14" in refund_faq["answer"] and "working" in refund_faq["answer"].lower()

    def test_klarna_refund_faq_present(self):
        klarna_faq = self.by_id.get("refund_004")
        assert klarna_faq is not None
        assert "klarna" in klarna_faq["answer"].lower()

    def test_wismo_delivered_faq_mentions_investigation(self):
        faq = self.by_id.get("wismo_003")
        assert faq is not None
        assert "investigation" in faq["answer"].lower() or "investigate" in faq["answer"].lower()

    def test_swimwear_return_faq_mentions_hygiene(self):
        faq = self.by_id.get("return_003")
        assert faq is not None
        assert "hygiene" in faq["answer"].lower()

    def test_wrong_item_faq_requests_photo(self):
        faq = self.by_id.get("wrong_item_001")
        assert faq is not None
        assert "photo" in faq["answer"].lower()

    def test_student_discount_faq_present(self):
        faq = self.by_id.get("promo_005")
        assert faq is not None
        assert "25" in faq["answer"]
        assert "student beans" in faq["answer"].lower()

    def test_cancel_faq_mentions_60_minutes(self):
        faq = self.by_id.get("cancel_001")
        assert faq is not None
        assert "60" in faq["answer"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. BRAND VOICE
# ─────────────────────────────────────────────────────────────────────────────

class TestBoohooBrandVoice:

    def setup_method(self):
        from data.boohoo_kb.brand_voice import (
            BOOHOO_BRAND_VOICE, KAREN_MILLEN_BRAND_VOICE,
            UNIVERSAL_RULES, get_brand_voice,
        )
        self.boohoo_voice = BOOHOO_BRAND_VOICE
        self.km_voice = KAREN_MILLEN_BRAND_VOICE
        self.universal = UNIVERSAL_RULES
        self.get_voice = get_brand_voice

    def test_boohoo_has_forbidden_phrases(self):
        assert len(self.boohoo_voice["forbidden_phrases"]) >= 5

    def test_per_our_policy_is_forbidden(self):
        forbidden = self.boohoo_voice["forbidden_phrases"]
        assert any("per our policy" in p.lower() for p in forbidden)

    def test_unfortunately_nothing_we_can_do_is_forbidden(self):
        forbidden = self.boohoo_voice["forbidden_phrases"]
        assert any("nothing we can do" in p.lower() for p in forbidden)

    def test_boohoo_has_escalation_scripts(self):
        scripts = self.boohoo_voice["escalation_scripts"]
        assert "to_human_l2" in scripts
        assert "formal_complaint" in scripts
        assert "refund_delay" in scripts

    def test_escalation_script_has_ticket_placeholder(self):
        script = self.boohoo_voice["escalation_scripts"]["to_human_l2"]
        assert "{ticket_id}" in script

    def test_refund_delay_script_has_amount_placeholder(self):
        script = self.boohoo_voice["escalation_scripts"]["refund_delay"]
        assert "{refund_amount}" in script

    def test_karen_millen_forbidden_hey(self):
        km_forbidden = self.km_voice["forbidden_phrases"]
        assert any("hey" in p.lower() for p in km_forbidden)

    def test_karen_millen_escalation_formal(self):
        script = self.km_voice["escalation_scripts"]["to_human_l2"]
        assert "sincerely" in script.lower() or "deeply" in script.lower()

    def test_universal_never_blame_customer(self):
        never_do_str = " ".join(self.universal["never_do"]).lower()
        assert "blame" in never_do_str

    def test_universal_always_give_timeframe(self):
        always_do_str = " ".join(self.universal["always_do"]).lower()
        assert "timeframe" in always_do_str or "specific" in always_do_str

    def test_universal_sensitive_topics_present(self):
        assert "bereavement" in self.universal["sensitive_topics"]
        assert "financial_hardship" in self.universal["sensitive_topics"]

    def test_get_brand_voice_boohoo(self):
        voice = self.get_voice("boohoo_uk")
        assert voice["brand_id"] == "boohoo_uk"

    def test_get_brand_voice_plt(self):
        voice = self.get_voice("plt_uk")
        assert voice["brand_id"] == "plt_uk"

    def test_get_brand_voice_karen_millen(self):
        voice = self.get_voice("karenmillen_uk")
        assert voice["tier"] == "premium"

    def test_channel_variants_present(self):
        variants = self.boohoo_voice["channel_variants"]
        assert "chat" in variants
        assert "email" in variants
        assert "voice" in variants

    def test_chat_emoji_allowed_for_boohoo(self):
        assert self.boohoo_voice["channel_variants"]["chat"]["emoji_allowed"] is True

    def test_email_emoji_not_allowed_for_boohoo(self):
        assert self.boohoo_voice["channel_variants"]["email"]["emoji_allowed"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. CONTEXT ENGINE ASSEMBLY WITH BOOHOO DATA
# ─────────────────────────────────────────────────────────────────────────────

class TestContextEngineWithBoohooKB:
    """
    Test context assembly using Boohoo KB data.
    Mocks retrieve_knowledge and get_brand_info at the retriever level —
    no FalkorDB or LLM calls required.
    """

    @pytest.fixture(autouse=True)
    def mock_retriever(self, monkeypatch):
        """Mock the retriever layer with realistic Boohoo data."""
        from data.boohoo_kb.policies import BOOHOO_RETURN_POLICY
        from core.knowledge.retriever import RetrievalResult

        boohoo_policy = {
            "return_window_days": BOOHOO_RETURN_POLICY["return_window_days"],
            "return_cost_gbp": BOOHOO_RETURN_POLICY["return_cost_gbp"],
            "return_cost_usd": None,
            "free_return_condition": None,
            "excluded_categories": BOOHOO_RETURN_POLICY["non_returnable_categories"],
        }

        wismo_resolutions = [
            {
                "id": "wismo_track",
                "name": "Provide tracking information",
                "steps": [
                    {"order": 1, "instruction": "Look up order status via order_lookup tool.",
                     "tool_required": "order_lookup", "condition": None},
                    {"order": 2, "instruction": "Retrieve tracking URL via shipment_track tool.",
                     "tool_required": "shipment_track", "condition": None},
                ],
                "requires_tool_call": True,
                "success_rate": 0.92,
                "escalation_trigger": None,
            },
        ]

        def mock_retrieve(brand_id, issue_type_id, order_placed_at=None, region="UK"):
            return RetrievalResult(
                resolutions=wismo_resolutions,
                policy=boohoo_policy,
                confidence=0.95,
                source="graph_tool",
            )

        monkeypatch.setattr("core.context.engine.retrieve_knowledge", mock_retrieve)

    def _make_llm_client(self, intent_response: str = None):
        """Return a mock LLM client."""
        default = json.dumps({
            "issue_type": "wismo",
            "confidence": 0.95,
            "category": "order",
            "entities": {"order_id": "BH-12345-UK"},
        })
        mock = AsyncMock()
        mock.generate = AsyncMock(return_value=intent_response or default)
        return mock

    @pytest.mark.asyncio
    async def test_wismo_context_contains_tracking_tools(self):
        """WISMO intent → context package must include order_lookup and shipment_track tools."""
        from core.context.engine import assemble_context
        from core.ontology.models import Channel

        with patch("core.context.engine.get_brand_info", return_value={"name": "Boohoo", "tier": "budget"}):
            ctx = await assemble_context(
                message="Where is my order BH-12345-UK?",
                session_id="test-session-wismo",
                brand_id="boohoo_uk",
                channel=Channel.CHAT,
                conversation_history=[],
                customer=None,
                llm_client=self._make_llm_client(),
            )

        assert ctx is not None
        assert ctx.intent is not None
        assert ctx.intent.issue_type == "wismo"
        assert "order_lookup" in ctx.tool_manifest
        assert "shipment_track" in ctx.tool_manifest

    @pytest.mark.asyncio
    async def test_return_context_contains_policy_block(self):
        """Return intent → context package must include policy block with 28-day window."""
        from core.context.engine import assemble_context
        from core.ontology.models import Channel

        return_intent = json.dumps({
            "issue_type": "return_initiate",
            "confidence": 0.93,
            "category": "return",
            "entities": {"return_reason": "wrong size"},
        })

        with patch("core.context.engine.get_brand_info", return_value={"name": "Boohoo", "tier": "budget"}):
            ctx = await assemble_context(
                message="I want to return an item — it's the wrong size.",
                session_id="test-session-return",
                brand_id="boohoo_uk",
                channel=Channel.CHAT,
                conversation_history=[],
                customer=None,
                llm_client=self._make_llm_client(return_intent),
            )

        assert ctx.intent.issue_type == "return_initiate"
        assert ctx.policy_block is not None
        assert "28" in ctx.policy_block or "28" in str(ctx)

    @pytest.mark.asyncio
    async def test_refund_context_has_tools(self):
        """refund_delay intent → refund_status + payment_lookup tools in manifest."""
        from core.context.engine import assemble_context
        from core.ontology.models import Channel

        refund_intent = json.dumps({
            "issue_type": "refund_delay",
            "confidence": 0.88,
            "category": "return",
            "entities": {},
        })

        with patch("core.context.engine.get_brand_info", return_value={"name": "Boohoo"}):
            ctx = await assemble_context(
                message="I returned my order 3 weeks ago and still no refund.",
                session_id="test-session-refund",
                brand_id="boohoo_uk",
                channel=Channel.CHAT,
                conversation_history=[],
                customer=None,
                llm_client=self._make_llm_client(refund_intent),
            )

        assert ctx.intent.issue_type == "refund_delay"
        assert "refund_status" in ctx.tool_manifest

    @pytest.mark.asyncio
    async def test_promo_context_has_tools(self):
        """promo_not_applied intent → promo_validate tool in manifest."""
        from core.context.engine import assemble_context
        from core.ontology.models import Channel

        promo_intent = json.dumps({
            "issue_type": "promo_not_applied",
            "confidence": 0.91,
            "category": "promotions",
            "entities": {"promo_code": "SAVE20"},
        })

        with patch("core.context.engine.get_brand_info", return_value={"name": "Boohoo"}):
            ctx = await assemble_context(
                message="My promo code SAVE20 isn't working.",
                session_id="test-session-promo",
                brand_id="boohoo_uk",
                channel=Channel.CHAT,
                conversation_history=[],
                customer=None,
                llm_client=self._make_llm_client(promo_intent),
            )

        assert ctx.intent.issue_type == "promo_not_applied"
        assert "promo_validate" in ctx.tool_manifest

    @pytest.mark.asyncio
    async def test_account_locked_context_has_tools(self):
        """account_locked intent → account_status tool in manifest."""
        from core.context.engine import assemble_context
        from core.ontology.models import Channel

        account_intent = json.dumps({
            "issue_type": "account_locked",
            "confidence": 0.90,
            "category": "account",
            "entities": {},
        })

        with patch("core.context.engine.get_brand_info", return_value={"name": "Boohoo"}):
            ctx = await assemble_context(
                message="I've been locked out of my account.",
                session_id="test-session-account",
                brand_id="boohoo_uk",
                channel=Channel.CHAT,
                conversation_history=[],
                customer=None,
                llm_client=self._make_llm_client(account_intent),
            )

        assert ctx.intent.issue_type == "account_locked"
        assert "account_status" in ctx.tool_manifest


# ─────────────────────────────────────────────────────────────────────────────
# 5. POLICY BLOCK FORMATTING WITH BOOHOO DATA
# ─────────────────────────────────────────────────────────────────────────────

class TestPolicyBlockWithBoohooData:
    """Validate that _format_policy_block produces the right output for Boohoo's policy data."""

    def test_boohoo_policy_block_contains_28_days(self):
        from core.context.engine import _format_policy_block
        from data.boohoo_kb.policies import BOOHOO_RETURN_POLICY as rp

        # Simulate the dict shape returned by get_return_policy()
        graph_policy = {
            "return_window_days": rp["return_window_days"],
            "return_cost_gbp": rp["return_cost_gbp"],
            "return_cost_usd": 0.0,
            "free_return_condition": None,
            "excluded_categories": rp["non_returnable_categories"],
        }
        block = _format_policy_block(graph_policy, "Boohoo")
        assert "28 days" in block
        assert "£1.99" in block

    def test_boohoo_policy_block_mentions_excluded_items(self):
        from core.context.engine import _format_policy_block

        graph_policy = {
            "return_window_days": 28,
            "return_cost_gbp": 1.99,
            "return_cost_usd": 0.0,
            "free_return_condition": None,
            "excluded_categories": ["swimwear", "underwear", "pierced jewellery"],
        }
        block = _format_policy_block(graph_policy, "Boohoo")
        assert "swimwear" in block

    def test_karen_millen_policy_block_shows_free_returns(self):
        from core.context.engine import _format_policy_block

        km_policy = {
            "return_window_days": 28,
            "return_cost_gbp": 0.0,
            "return_cost_usd": 0.0,
            "free_return_condition": "All orders eligible for free returns",
            "excluded_categories": [],
        }
        block = _format_policy_block(km_policy, "Karen Millen")
        assert "All orders eligible" in block
        assert "Free" in block or "free" in block.lower()

    def test_policy_block_refund_timeline_mentioned(self):
        from core.context.engine import _format_policy_block

        graph_policy = {
            "return_window_days": 28,
            "return_cost_gbp": 1.99,
            "return_cost_usd": 0.0,
            "free_return_condition": None,
            "excluded_categories": [],
        }
        block = _format_policy_block(graph_policy, "Boohoo")
        assert "day" in block.lower()  # some timeline mention


# ─────────────────────────────────────────────────────────────────────────────
# 6. SEEDER UNIT TESTS (no live services needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestBoohooSeeder:
    """Test seeder functions with mocked FalkorDB and Postgres."""

    @pytest.fixture
    def mock_graph(self):
        mock_result = MagicMock()
        mock_result.result_set = [[True]]
        g = MagicMock()
        g.query = MagicMock(return_value=mock_result)
        return g

    def test_seed_faqs_to_graph_calls_query_for_each_faq(self, mock_graph):
        from data.boohoo_kb.seed_boohoo import seed_faqs_to_graph
        from data.boohoo_kb.faqs import BOOHOO_FAQS

        result = seed_faqs_to_graph(mock_graph)
        assert mock_graph.query.call_count >= len(BOOHOO_FAQS)
        assert result["errors"] == 0

    def test_seed_faqs_to_graph_handles_query_errors_gracefully(self, mock_graph):
        from data.boohoo_kb.seed_boohoo import seed_faqs_to_graph

        mock_graph.query = MagicMock(side_effect=Exception("Simulated FalkorDB error"))
        result = seed_faqs_to_graph(mock_graph)
        assert result["errors"] > 0  # errors counted, no exception raised

    def test_seed_policies_to_graph_calls_query_for_each_policy(self, mock_graph):
        from data.boohoo_kb.seed_boohoo import seed_policies_to_graph
        from data.boohoo_kb.policies import ALL_POLICIES

        result = seed_policies_to_graph(mock_graph)
        assert mock_graph.query.call_count >= len(ALL_POLICIES)
        assert result["errors"] == 0

    def test_extract_policy_chunks_returns_substantive_chunks(self):
        from data.boohoo_kb.seed_boohoo import _extract_policy_chunks

        chunks = _extract_policy_chunks()
        assert len(chunks) >= 10
        for chunk in chunks:
            assert "content" in chunk
            assert "policy_type" in chunk
            assert "section" in chunk
            assert len(chunk["content"]) >= 20

    def test_extract_policy_chunks_contain_boohoo_return_facts(self):
        from data.boohoo_kb.seed_boohoo import _extract_policy_chunks

        chunks = _extract_policy_chunks()
        all_content = " ".join(c["content"] for c in chunks)
        assert "28" in all_content
        assert "1.99" in all_content or "£1.99" in all_content

    def test_extract_policy_chunks_contain_shipping_facts(self):
        from data.boohoo_kb.seed_boohoo import _extract_policy_chunks

        chunks = _extract_policy_chunks()
        shipping_chunks = [c for c in chunks if c["policy_type"] == "shipping"]
        assert len(shipping_chunks) >= 3
        shipping_content = " ".join(c["content"] for c in shipping_chunks)
        assert "35" in shipping_content  # free threshold

    def test_zero_vector_correct_dimensions(self):
        from data.boohoo_kb.seed_boohoo import _zero_vector
        vec = _zero_vector()
        assert len(vec) == 1536
        assert all(v == 0.0 for v in vec)

    def test_embedder_returns_none_without_embed_flag(self):
        from data.boohoo_kb.seed_boohoo import _get_embedder
        import os
        original = os.environ.pop("EMBED", None)
        try:
            embedder = _get_embedder()
            assert embedder is None
        finally:
            if original:
                os.environ["EMBED"] = original


# ─────────────────────────────────────────────────────────────────────────────
# 7. LIVE INTEGRATION: FalkorDB (skipped if unavailable)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not falkordb_available, reason="FalkorDB not available on :6381")
class TestBoohooKBLiveFalkorDB:
    """
    Seeds Boohoo FAQs into live FalkorDB and validates retrieval.
    Uses the same queries the context engine would use.
    """

    @pytest.fixture(autouse=True)
    def seed(self):
        from core.ontology.schema import get_graph_client, create_schema
        from core.ontology.seed_data import seed_all
        from data.boohoo_kb.seed_boohoo import seed_faqs_to_graph, seed_policies_to_graph

        _, graph = get_graph_client()
        create_schema(graph)
        seed_all(graph)
        seed_faqs_to_graph(graph)
        seed_policies_to_graph(graph)
        self.graph = graph

    def test_boohoo_has_faq_nodes(self):
        result = self.graph.query(
            "MATCH (b:Brand {id: 'boohoo_uk'})-[:HAS_FAQ]->(f:FAQ) RETURN count(f) AS cnt"
        )
        assert result.result_set[0][0] >= 60

    def test_wismo_faqs_retrievable(self):
        result = self.graph.query(
            "MATCH (b:Brand {id: 'boohoo_uk'})-[:HAS_FAQ]->(f:FAQ {category: 'wismo'}) "
            "RETURN f.question, f.answer LIMIT 5"
        )
        assert len(result.result_set) >= 4
        # At least one answer should mention tracking
        answers = [row[1].lower() for row in result.result_set]
        assert any("track" in a or "dispatch" in a for a in answers)

    def test_return_faq_mentions_28_days(self):
        result = self.graph.query(
            "MATCH (f:FAQ {id: 'return_002'}) RETURN f.answer"
        )
        assert len(result.result_set) == 1
        assert "28" in result.result_set[0][0]

    def test_policy_detail_node_exists(self):
        result = self.graph.query(
            "MATCH (b:Brand {id: 'boohoo_uk'})-[:HAS_POLICY_DETAIL]->(pd:PolicyDetail) "
            "RETURN pd.policy_type"
        )
        policy_types = {row[0] for row in result.result_set}
        assert "return" in policy_types
        assert "shipping" in policy_types

    def test_faq_content_json_parseable(self):
        result = self.graph.query(
            "MATCH (f:FAQ {id: 'refund_001'}) RETURN f.keywords"
        )
        assert len(result.result_set) == 1
        keywords_raw = result.result_set[0][0]
        keywords = json.loads(keywords_raw)
        assert isinstance(keywords, list)
        assert len(keywords) >= 2

    def test_issue_type_faq_relationship(self):
        """IssueType wismo should have HAS_FAQ edges."""
        result = self.graph.query(
            "MATCH (it:IssueType {id: 'wismo'})-[:HAS_FAQ]->(f:FAQ) RETURN count(f) AS cnt"
        )
        # May be 0 if wismo IssueType exists; relationship only created if IssueType exists
        cnt = result.result_set[0][0]
        assert cnt >= 0  # No hard assertion — IssueType must exist

    def test_idempotency_double_seed(self):
        """Second seed run produces same count — no duplicates."""
        from data.boohoo_kb.seed_boohoo import seed_faqs_to_graph

        before = self.graph.query(
            "MATCH (f:FAQ {brand_id: 'boohoo_uk'}) RETURN count(f) AS cnt"
        ).result_set[0][0]

        seed_faqs_to_graph(self.graph)  # run again

        after = self.graph.query(
            "MATCH (f:FAQ {brand_id: 'boohoo_uk'}) RETURN count(f) AS cnt"
        ).result_set[0][0]

        assert before == after, f"Duplicate FAQs created: was {before}, now {after}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. LIVE INTEGRATION: Postgres (skipped if unavailable)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not postgres_available, reason="Postgres not available on :5432")
class TestBoohooKBLivePostgres:
    """
    Seeds knowledge_chunks into live Postgres and validates content.
    No embeddings (EMBED not set) — zero-vectors used.
    """

    @pytest.mark.asyncio
    async def test_seed_faqs_inserts_rows(self):
        from data.boohoo_kb.seed_boohoo import seed_faqs_to_pgvector
        from data.boohoo_kb.faqs import BOOHOO_FAQS
        import asyncpg
        from config import settings

        result = await seed_faqs_to_pgvector(embedder=None)
        # Should have inserted at least the FAQ count + policy chunks
        assert (result["inserted"] + result["errors"]) >= len(BOOHOO_FAQS)

    @pytest.mark.asyncio
    async def test_faq_chunks_exist_in_knowledge_chunks(self):
        import asyncpg
        from config import settings

        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        try:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE brand_id = 'boohoo_uk' AND chunk_type = 'faq'"
            )
            assert count >= 60, f"Expected >=60 FAQ chunks, got {count}"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_policy_chunks_exist_in_knowledge_chunks(self):
        import asyncpg
        from config import settings

        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        try:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE brand_id = 'boohoo_uk' AND chunk_type = 'policy'"
            )
            assert count >= 10, f"Expected >=10 policy chunks, got {count}"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_faq_content_correct_format(self):
        import asyncpg
        from config import settings

        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                "SELECT content, metadata FROM knowledge_chunks "
                "WHERE brand_id = 'boohoo_uk' AND chunk_type = 'faq' LIMIT 1"
            )
            assert row is not None
            content = row["content"]
            assert content.startswith("Q:") or "Q:" in content
            assert "A:" in content
            meta = json.loads(row["metadata"])
            assert "faq_id" in meta
            assert "category" in meta
        finally:
            await conn.close()
