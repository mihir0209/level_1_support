"""
Validation layer for extracted facts.

Confidence thresholds:
  >= 0.85  → AUTO_APPROVE  (write directly to knowledge graph)
  0.70-0.84 → HUMAN_REVIEW  (queue for human review in Postgres)
  < 0.70   → REJECT         (discard)

Also checks:
  - Required fields not empty
  - Temporal consistency (valid_from < valid_until)
  - No duplicate detection (hash-based)
  - Policy type is a known category
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

from ingestion.extractor import ExtractedFacts, ExtractedPolicy, ExtractedFAQ, ExtractedBrandFact

log = structlog.get_logger(__name__)

_AUTO_APPROVE_THRESHOLD = 0.85
_HUMAN_REVIEW_THRESHOLD = 0.70

_VALID_POLICY_TYPES = {"return", "shipping", "payment", "cancellation", "warranty", "general"}
_VALID_INTENT_TAGS = {"wismo", "return", "refund", "payment", "product", "account", "promo"}


class ValidationOutcome(str, Enum):
    AUTO_APPROVE = "auto_approve"
    HUMAN_REVIEW = "human_review"
    REJECT = "reject"


@dataclass
class ValidationResult:
    outcome: ValidationOutcome
    item_type: str        # "policy" | "faq" | "brand_fact"
    item_data: dict[str, Any]
    brand_id: str
    source_url: str
    confidence: float
    rejection_reason: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "item_type": self.item_type,
            "item_data": self.item_data,
            "brand_id": self.brand_id,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "rejection_reason": self.rejection_reason,
            "content_hash": self.content_hash,
        }


def _hash_content(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _validate_policy(
    policy: ExtractedPolicy,
    brand_id: str,
    source_url: str,
) -> ValidationResult:
    """Validate a single extracted policy."""
    item_data = {
        "type": policy.type,
        "summary": policy.summary,
        "conditions": policy.conditions,
        "valid_from": policy.valid_from,
        "valid_until": policy.valid_until,
        "regions": policy.regions,
    }
    content_hash = _hash_content(item_data)

    # Required field checks
    if not policy.summary or len(policy.summary.strip()) < 10:
        return ValidationResult(
            outcome=ValidationOutcome.REJECT,
            item_type="policy",
            item_data=item_data,
            brand_id=brand_id,
            source_url=source_url,
            confidence=policy.confidence,
            rejection_reason="summary too short or empty",
            content_hash=content_hash,
        )

    if policy.type not in _VALID_POLICY_TYPES:
        item_data["type"] = "general"  # normalise unknown types

    # Temporal consistency
    if policy.valid_from and policy.valid_until:
        if policy.valid_from > policy.valid_until:
            return ValidationResult(
                outcome=ValidationOutcome.REJECT,
                item_type="policy",
                item_data=item_data,
                brand_id=brand_id,
                source_url=source_url,
                confidence=policy.confidence,
                rejection_reason="valid_from is after valid_until",
                content_hash=content_hash,
            )

    outcome = _threshold_outcome(policy.confidence)
    return ValidationResult(
        outcome=outcome,
        item_type="policy",
        item_data=item_data,
        brand_id=brand_id,
        source_url=source_url,
        confidence=policy.confidence,
        content_hash=content_hash,
    )


def _validate_faq(
    faq: ExtractedFAQ,
    brand_id: str,
    source_url: str,
) -> ValidationResult:
    """Validate a single extracted FAQ."""
    item_data = {
        "question": faq.question,
        "answer": faq.answer,
        "intent_tags": faq.intent_tags,
        "suggested_categories": faq.suggested_categories,
    }
    content_hash = _hash_content(item_data)

    if not faq.question or len(faq.question.strip()) < 5:
        return ValidationResult(
            outcome=ValidationOutcome.REJECT,
            item_type="faq",
            item_data=item_data,
            brand_id=brand_id,
            source_url=source_url,
            confidence=faq.confidence,
            rejection_reason="question too short or empty",
            content_hash=content_hash,
        )
    if not faq.answer or len(faq.answer.strip()) < 10:
        return ValidationResult(
            outcome=ValidationOutcome.REJECT,
            item_type="faq",
            item_data=item_data,
            brand_id=brand_id,
            source_url=source_url,
            confidence=faq.confidence,
            rejection_reason="answer too short or empty",
            content_hash=content_hash,
        )

    outcome = _threshold_outcome(faq.confidence)
    return ValidationResult(
        outcome=outcome,
        item_type="faq",
        item_data=item_data,
        brand_id=brand_id,
        source_url=source_url,
        confidence=faq.confidence,
        content_hash=content_hash,
    )


def _validate_brand_fact(
    bf: ExtractedBrandFact,
    brand_id: str,
    source_url: str,
) -> ValidationResult:
    item_data = {"fact_type": bf.fact_type, "content": bf.content}
    content_hash = _hash_content(item_data)

    if not bf.content or len(bf.content.strip()) < 10:
        return ValidationResult(
            outcome=ValidationOutcome.REJECT,
            item_type="brand_fact",
            item_data=item_data,
            brand_id=brand_id,
            source_url=source_url,
            confidence=bf.confidence,
            rejection_reason="content too short or empty",
            content_hash=content_hash,
        )

    outcome = _threshold_outcome(bf.confidence)
    return ValidationResult(
        outcome=outcome,
        item_type="brand_fact",
        item_data=item_data,
        brand_id=brand_id,
        source_url=source_url,
        confidence=bf.confidence,
        content_hash=content_hash,
    )


def _threshold_outcome(confidence: float) -> ValidationOutcome:
    if confidence >= _AUTO_APPROVE_THRESHOLD:
        return ValidationOutcome.AUTO_APPROVE
    elif confidence >= _HUMAN_REVIEW_THRESHOLD:
        return ValidationOutcome.HUMAN_REVIEW
    return ValidationOutcome.REJECT


@dataclass
class ValidationReport:
    brand_id: str
    source_url: str
    auto_approve: list[ValidationResult] = field(default_factory=list)
    human_review: list[ValidationResult] = field(default_factory=list)
    rejected: list[ValidationResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.auto_approve) + len(self.human_review) + len(self.rejected)

    def summary(self) -> dict[str, int]:
        return {
            "auto_approve": len(self.auto_approve),
            "human_review": len(self.human_review),
            "rejected": len(self.rejected),
            "total": self.total,
        }


def validate_facts(facts: ExtractedFacts) -> ValidationReport:
    """
    Validate all items in an ExtractedFacts object.

    Returns a ValidationReport sorted into auto_approve / human_review / rejected buckets.
    """
    report = ValidationReport(brand_id=facts.brand_id, source_url=facts.source_url)

    all_results: list[ValidationResult] = []

    for policy in facts.policies:
        all_results.append(_validate_policy(policy, facts.brand_id, facts.source_url))

    for faq in facts.faqs:
        all_results.append(_validate_faq(faq, facts.brand_id, facts.source_url))

    for bf in facts.brand_facts:
        all_results.append(_validate_brand_fact(bf, facts.brand_id, facts.source_url))

    # Deduplicate by content_hash within this batch (keep highest-confidence version)
    seen_hashes: dict[str, ValidationResult] = {}
    for result in all_results:
        h = result.content_hash
        if h not in seen_hashes or result.confidence > seen_hashes[h].confidence:
            seen_hashes[h] = result

    for result in seen_hashes.values():
        if result.outcome == ValidationOutcome.AUTO_APPROVE:
            report.auto_approve.append(result)
        elif result.outcome == ValidationOutcome.HUMAN_REVIEW:
            report.human_review.append(result)
        else:
            report.rejected.append(result)

    log.info(
        "validation_complete",
        brand_id=facts.brand_id,
        **report.summary(),
    )
    return report
