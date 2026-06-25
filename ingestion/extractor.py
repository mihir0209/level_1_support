"""
LLM-based policy and FAQ extractor.

Takes a CrawledPage (markdown) and produces structured ExtractedFacts
that can be validated and upserted into the knowledge graph.

Extraction schema:
  - policies: return/shipping/payment policies with temporal attributes
  - faqs: Q&A pairs with intent tags
  - brand_facts: general brand info (contact hours, channels, etc.)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from config import settings
from core.orchestration.llm import get_llm_client
from ingestion.crawler import CrawledPage

log = structlog.get_logger(__name__)

# ── Extraction prompts (built dynamically per domain config) ──────────────────

def build_system_prompt(domain_cfg: Any) -> str:
    domain_name = domain_cfg.domain_name
    return f"""You are a data extraction specialist for {domain_name} support knowledge bases.
Extract structured facts from the provided support page markdown.
Return ONLY valid JSON — no explanation, no markdown fences.
All monetary/numeric values should be in the format shown.
Dates should be ISO 8601. Unknown values use null."""


def build_extraction_prompt(brand_id: str, url: str, title: str, content: str, domain_cfg: Any) -> str:
    domain_name = domain_cfg.domain_name
    intent_tags = [it_id for it_id in domain_cfg.issue_type_ids if it_id not in ("info", "unknown")]

    # Define a clean list of policy types based on the categories
    policy_types = ["general", "cancellation", "terms", "sla"]
    if domain_cfg.domain_id == "ecommerce":
        policy_types.extend(["return", "shipping", "payment", "warranty"])
    elif domain_cfg.domain_id == "telecom":
        policy_types.extend(["billing", "network", "usage", "device"])

    policy_types_str = "|".join(policy_types)
    intent_tags_str = ", ".join(f'"{t}"' for t in intent_tags)

    return f"""Extract all support-relevant facts from this {domain_name} support page.
Brand: {brand_id}
URL: {url}
Page title: {title}

--- CONTENT ---
{content}
--- END ---

Return JSON with this exact structure:
{{
  "policies": [
    {{
      "type": "{policy_types_str}",
      "summary": "one-sentence summary",
      "conditions": [{{"condition": "str", "value": "str"}}],
      "valid_from": "ISO date or null",
      "valid_until": "ISO date or null",
      "regions": ["UK", "US", "EU", "international", "all"],
      "confidence": 0.0-1.0
    }}
  ],
  "faqs": [
    {{
      "question": "str",
      "answer": "str",
      "intent_tags": [{intent_tags_str}],
      "suggested_categories": ["new_category_slug"],
      "confidence": 0.0-1.0
    }}
  ],
  "brand_facts": [
    {{
      "fact_type": "contact_hours|channel|sla|policy_change|general",
      "content": "str",
      "confidence": 0.0-1.0
    }}
  ]
}}

If a Q&A pair does not fit any standard intent tags, suggest a new, brand-specific support category slug in suggested_categories (e.g., "network_troubleshooting", "account_suspension", etc.)."""


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ExtractedPolicy:
    type: str
    summary: str
    conditions: list[dict[str, str]] = field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None
    regions: list[str] = field(default_factory=lambda: ["all"])
    confidence: float = 0.0


@dataclass
class ExtractedFAQ:
    question: str
    answer: str
    intent_tags: list[str] = field(default_factory=list)
    suggested_categories: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class ExtractedBrandFact:
    fact_type: str
    content: str
    confidence: float = 0.0


@dataclass
class ExtractedFacts:
    brand_id: str
    source_url: str
    extracted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    policies: list[ExtractedPolicy] = field(default_factory=list)
    faqs: list[ExtractedFAQ] = field(default_factory=list)
    brand_facts: list[ExtractedBrandFact] = field(default_factory=list)

    @property
    def total_items(self) -> int:
        return len(self.policies) + len(self.faqs) + len(self.brand_facts)

    @property
    def high_confidence_items(self) -> int:
        """Count items with confidence >= 0.85 (auto-approve threshold)."""
        count = 0
        for p in self.policies:
            if p.confidence >= 0.85:
                count += 1
        for f in self.faqs:
            if f.confidence >= 0.85:
                count += 1
        for b in self.brand_facts:
            if b.confidence >= 0.85:
                count += 1
        return count


# ═══════════════════════════════════════════════════════════════════════════


def _truncate_content(markdown: str, max_tokens: int = 6000) -> str:
    """Rough token estimate: ~4 chars/token. Truncate to stay within LLM context."""
    max_chars = max_tokens * 4
    if len(markdown) <= max_chars:
        return markdown
    # Preserve first and last sections (usually most policy-dense)
    half = max_chars // 2
    return markdown[:half] + "\n\n[...truncated...]\n\n" + markdown[-half:]


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Extract and parse JSON from LLM response (handles markdown fences)."""
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw.strip(), flags=re.MULTILINE)
    return json.loads(raw.strip())


def _parse_extracted(data: dict[str, Any], brand_id: str, url: str) -> ExtractedFacts:
    """Convert raw LLM JSON into ExtractedFacts dataclass."""
    facts = ExtractedFacts(brand_id=brand_id, source_url=url)

    for p in data.get("policies", []):
        try:
            facts.policies.append(ExtractedPolicy(
                type=str(p.get("type", "general")),
                summary=str(p.get("summary", "")),
                conditions=p.get("conditions", []),
                valid_from=p.get("valid_from"),
                valid_until=p.get("valid_until"),
                regions=p.get("regions", ["all"]),
                confidence=float(p.get("confidence", 0.5)),
            ))
        except (TypeError, ValueError) as exc:
            log.debug("policy_parse_skip", error=str(exc))

    for f in data.get("faqs", []):
        try:
            facts.faqs.append(ExtractedFAQ(
                question=str(f.get("question", "")),
                answer=str(f.get("answer", "")),
                intent_tags=f.get("intent_tags", []),
                suggested_categories=f.get("suggested_categories", []),
                confidence=float(f.get("confidence", 0.5)),
            ))
        except (TypeError, ValueError) as exc:
            log.debug("faq_parse_skip", error=str(exc))

    for b in data.get("brand_facts", []):
        try:
            facts.brand_facts.append(ExtractedBrandFact(
                fact_type=str(b.get("fact_type", "general")),
                content=str(b.get("content", "")),
                confidence=float(b.get("confidence", 0.5)),
            ))
        except (TypeError, ValueError) as exc:
            log.debug("brand_fact_parse_skip", error=str(exc))

    return facts


async def extract_facts(page: CrawledPage) -> ExtractedFacts:
    """
    Run LLM extraction on a crawled page.

    Uses the primary (non-fast) model for accuracy.
    Returns ExtractedFacts (may have 0 items if page is not relevant).
    """
    from brands.registry import get_brand_by_id
    from domains.loader import get_domain_config

    brand_entry = get_brand_by_id(page.brand_id)
    domain = brand_entry.domain if brand_entry else "ecommerce"
    domain_cfg = get_domain_config(domain)

    llm = get_llm_client()
    content = _truncate_content(page.markdown)

    prompt = build_extraction_prompt(
        brand_id=page.brand_id,
        url=page.url,
        title=page.title or "(no title)",
        content=content,
        domain_cfg=domain_cfg,
    )
    system_prompt = build_system_prompt(domain_cfg)

    try:
        raw = await llm.generate(
            prompt=prompt,
            system=system_prompt,
            max_tokens=4096,
            fast=False,
        )
        data = _parse_llm_json(raw)
        facts = _parse_extracted(data, page.brand_id, page.url)
        log.info(
            "extraction_done",
            url=page.url,
            brand_id=page.brand_id,
            policies=len(facts.policies),
            faqs=len(facts.faqs),
            brand_facts=len(facts.brand_facts),
        )
        return facts
    except json.JSONDecodeError as exc:
        log.error("extraction_json_error", url=page.url, error=str(exc))
        return ExtractedFacts(brand_id=page.brand_id, source_url=page.url)
    except Exception as exc:
        log.error("extraction_failed", url=page.url, error=str(exc))
        return ExtractedFacts(brand_id=page.brand_id, source_url=page.url)
