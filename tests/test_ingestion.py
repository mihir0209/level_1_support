"""
Tests for the ingestion pipeline.

Uses mocked Firecrawl and LLM calls — no external services required.
"""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════════
# Extractor tests
# ═══════════════════════════════════════════════════════════════════════════

class TestExtractor:

    @pytest.mark.asyncio
    async def test_extract_facts_valid_json(self, monkeypatch):
        """LLM returns well-formed JSON → ExtractedFacts has correct counts."""
        from ingestion.crawler import CrawledPage
        from ingestion import extractor as ext

        mock_llm_response = json.dumps({
            "policies": [
                {
                    "type": "return",
                    "summary": "28-day free returns for all orders over £30.",
                    "conditions": [{"condition": "min_order", "value": "30"}],
                    "valid_from": None,
                    "valid_until": None,
                    "regions": ["UK"],
                    "confidence": 0.92,
                }
            ],
            "faqs": [
                {
                    "question": "How long does delivery take?",
                    "answer": "Standard delivery takes 3-5 working days.",
                    "intent_tags": ["wismo"],
                    "confidence": 0.88,
                }
            ],
            "brand_facts": [
                {
                    "fact_type": "contact_hours",
                    "content": "Customer support is available Mon-Fri 9am-6pm GMT.",
                    "confidence": 0.95,
                }
            ],
        })

        class MockLLMClient:
            async def generate(self, prompt, system, max_tokens, fast):
                return mock_llm_response

        monkeypatch.setattr(ext, "get_llm_client", lambda: MockLLMClient())

        page = CrawledPage(
            url="https://boohoo.com/help/returns",
            brand_id="boohoo_uk",
            markdown="# Returns Policy\n28-day free returns for orders over £30.",
            title="Returns Policy",
        )
        facts = await ext.extract_facts(page)
        assert len(facts.policies) == 1
        assert facts.policies[0].type == "return"
        assert facts.policies[0].confidence == 0.92
        assert len(facts.faqs) == 1
        assert len(facts.brand_facts) == 1
        assert facts.total_items == 3
        assert facts.high_confidence_items == 3  # all >= 0.85

    @pytest.mark.asyncio
    async def test_extract_facts_suggested_categories(self, monkeypatch):
        """LLM returns suggested_categories → ExtractedFacts parses them correctly."""
        from ingestion.crawler import CrawledPage
        from ingestion import extractor as ext

        mock_llm_response = json.dumps({
            "policies": [],
            "faqs": [
                {
                    "question": "How to unlock plan?",
                    "answer": "Contact network team.",
                    "intent_tags": [],
                    "suggested_categories": ["plan_unlock", "network_trouble"],
                    "confidence": 0.89,
                }
            ],
            "brand_facts": [],
        })

        class MockLLMClient:
            async def generate(self, prompt, system, max_tokens, fast):
                return mock_llm_response

        monkeypatch.setattr(ext, "get_llm_client", lambda: MockLLMClient())

        page = CrawledPage(
            url="https://telecom.com/help",
            brand_id="telecom_brand",
            markdown="How to unlock plan",
            title="Help",
        )
        facts = await ext.extract_facts(page)
        assert len(facts.faqs) == 1
        assert facts.faqs[0].suggested_categories == ["plan_unlock", "network_trouble"]

    @pytest.mark.asyncio
    async def test_extract_facts_bad_json_returns_empty(self, monkeypatch):
        """If LLM returns invalid JSON → ExtractedFacts with 0 items (no exception)."""
        from ingestion.crawler import CrawledPage
        from ingestion import extractor as ext

        class MockLLMClient:
            async def generate(self, prompt, system, max_tokens, fast):
                return "I cannot extract facts from this page."

        monkeypatch.setattr(ext, "get_llm_client", lambda: MockLLMClient())

        page = CrawledPage(
            url="https://boohoo.com/not-relevant",
            brand_id="boohoo_uk",
            markdown="Buy now, pay later. Shop today.",
            title="Shop",
        )
        facts = await ext.extract_facts(page)
        assert facts.total_items == 0

    def test_truncate_content(self):
        from ingestion.extractor import _truncate_content
        short = "hello world"
        assert _truncate_content(short) == short
        long_content = "x" * 100_000
        truncated = _truncate_content(long_content, max_tokens=1000)
        assert len(truncated) < 100_000
        assert "[...truncated...]" in truncated


# ═══════════════════════════════════════════════════════════════════════════
# Validator tests
# ═══════════════════════════════════════════════════════════════════════════

class TestValidator:

    def _make_facts(self, brand_id="boohoo_uk", url="https://boohoo.com/help"):
        from ingestion.extractor import ExtractedFacts, ExtractedPolicy, ExtractedFAQ, ExtractedBrandFact
        facts = ExtractedFacts(brand_id=brand_id, source_url=url)
        facts.policies.append(ExtractedPolicy(
            type="return",
            summary="28-day return window for all items.",
            regions=["UK"],
            confidence=0.92,
        ))
        facts.faqs.append(ExtractedFAQ(
            question="How do I track my order?",
            answer="Use the tracking link in your dispatch email.",
            intent_tags=["wismo"],
            confidence=0.88,
        ))
        facts.faqs.append(ExtractedFAQ(
            question="What is your return policy?",
            answer="You have 28 days to return items.",
            intent_tags=["return"],
            confidence=0.75,  # human review threshold
        ))
        facts.faqs.append(ExtractedFAQ(
            question="?",
            answer="ok",
            intent_tags=[],
            confidence=0.80,
        ))
        return facts

    def test_auto_approve_threshold(self):
        from ingestion.validator import validate_facts, ValidationOutcome
        facts = self._make_facts()
        report = validate_facts(facts)
        auto_ids = [r.confidence for r in report.auto_approve]
        for c in auto_ids:
            assert c >= 0.85

    def test_human_review_threshold(self):
        from ingestion.validator import validate_facts
        facts = self._make_facts()
        report = validate_facts(facts)
        for r in report.human_review:
            assert 0.70 <= r.confidence < 0.85

    def test_reject_too_short(self):
        from ingestion.validator import validate_facts, ValidationOutcome
        from ingestion.extractor import ExtractedFacts, ExtractedFAQ
        facts = ExtractedFacts(brand_id="boohoo_uk", source_url="https://example.com")
        # Question is too short (< 5 chars)
        facts.faqs.append(ExtractedFAQ(question="?", answer="ok", intent_tags=[], confidence=0.99))
        report = validate_facts(facts)
        assert len(report.rejected) == 1
        assert report.rejected[0].rejection_reason == "question too short or empty"

    def test_invalid_temporal_policy_rejected(self):
        from ingestion.validator import validate_facts
        from ingestion.extractor import ExtractedFacts, ExtractedPolicy
        facts = ExtractedFacts(brand_id="boohoo_uk", source_url="https://example.com")
        facts.policies.append(ExtractedPolicy(
            type="return",
            summary="This policy has reversed dates.",
            valid_from="2025-12-31",
            valid_until="2025-01-01",
            confidence=0.95,
        ))
        report = validate_facts(facts)
        rejected = [r for r in report.rejected if "valid_from" in r.rejection_reason]
        assert len(rejected) == 1

    def test_deduplication(self):
        """Same content hash twice should only keep one item."""
        from ingestion.validator import validate_facts
        from ingestion.extractor import ExtractedFacts, ExtractedFAQ
        facts = ExtractedFacts(brand_id="boohoo_uk", source_url="https://example.com")
        faq = ExtractedFAQ(
            question="How long does delivery take?",
            answer="Standard delivery takes 3-5 working days.",
            intent_tags=["wismo"],
            confidence=0.90,
        )
        facts.faqs.append(faq)
        facts.faqs.append(faq)  # exact duplicate
        report = validate_facts(facts)
        total = report.total
        assert total == 1

    def test_report_summary_counts(self):
        from ingestion.validator import validate_facts
        facts = self._make_facts()
        report = validate_facts(facts)
        summary = report.summary()
        assert summary["total"] == report.total
        assert summary["auto_approve"] + summary["human_review"] + summary["rejected"] == summary["total"]

    def test_suggested_categories_validation_preserved(self):
        from ingestion.validator import validate_facts
        from ingestion.extractor import ExtractedFacts, ExtractedFAQ

        facts = ExtractedFacts(brand_id="boohoo_uk", source_url="https://boohoo.com")
        facts.faqs.append(ExtractedFAQ(
            question="What is this?",
            answer="This is a test Q&A pair.",
            intent_tags=["custom_tag"],
            suggested_categories=["suggested_tag"],
            confidence=0.90,
        ))

        report = validate_facts(facts)
        assert len(report.auto_approve) == 1
        validated_faq = report.auto_approve[0]
        assert validated_faq.item_data["intent_tags"] == ["custom_tag"]
        assert validated_faq.item_data["suggested_categories"] == ["suggested_tag"]



# ═══════════════════════════════════════════════════════════════════════════
# Crawler URL filter tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCrawlerFilters:

    def test_relevant_urls_pass(self):
        from ingestion.crawler import _is_relevant
        domain = "boohoo.com"
        relevant = [
            "https://boohoo.com/help/returns",
            "https://boohoo.com/faq",
            "https://boohoo.com/delivery-information",
            "https://boohoo.com/help/refunds",
        ]
        for url in relevant:
            assert _is_relevant(url, domain), f"Expected {url} to be relevant"

    def test_irrelevant_urls_filtered(self):
        from ingestion.crawler import _is_relevant
        domain = "boohoo.com"
        irrelevant = [
            "https://boohoo.com/checkout",
            "https://boohoo.com/cart",
            "https://boohoo.com/login",
            "https://boohoo.com/cdn/images/hero.jpg",
        ]
        for url in irrelevant:
            assert not _is_relevant(url, domain), f"Expected {url} to be filtered"

    def test_cross_domain_filtered(self):
        from ingestion.crawler import _is_relevant
        # URL from different domain should be filtered
        assert not _is_relevant("https://otherbrand.com/help/returns", "boohoo.com")
