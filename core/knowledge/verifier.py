"""Knowledge Base policy conflict analysis verifier."""

from __future__ import annotations

import json
import structlog
from core.orchestration.llm import get_llm_client

log = structlog.get_logger(__name__)


async def verify_policy_conflicts(brand_id: str, new_policy_text: str) -> list[dict]:
    """
    Compare new proposed policy rule text against existing active policies or rule sets.
    Returns a list of detected contradiction/conflict dicts.
    """
    # Active reference rules representing organizational truth
    existing_policies = [
        "Returns are permitted within 28 days of order delivery for standard items.",
        "Returns for swimwear and underwear are strictly excluded from returns policy.",
        "Wisconsin/WisMO tracking requests have a shipping time window estimate of 3-5 business days.",
        "Standard delivery orders are shipped via EVRI and take 2-4 business days.",
        "Refund processing takes up to 14 days once the warehouse registers the returned package."
    ]

    existing_text = "\n".join([f"- {p}" for p in existing_policies])

    prompt = f"""
    You are an ontological policy verifier. Check the following new policy proposal against the existing active organizational truth to detect contradictions or direct conflicts.

    EXISTING ACTIVE POLICIES:
    {existing_text}

    NEW PROPOSAL:
    {new_policy_text}

    CONSTRAINTS & RULES FOR CONFLICTS:
    - Return window conflicts (e.g. 14 days vs 28 days).
    - Product exclusion contradictions (e.g. underwear is allowed for return vs underwear excluded).
    - Shipping/delivery window mismatches.
    - Fee/monetary contradictions.

    Respond with ONLY a JSON list of objects containing details of each conflict. If no conflict is found, return an empty list: [].
    Do NOT include markdown wrapping or conversational chatter.

    JSON Scheme:
    [
      {{
        "conflict_type": "contradiction | mismatch | rule_violation",
        "existing_rule": "The exact active rule that conflicts",
        "proposed_rule": "The exact proposed rule that conflicts",
        "explanation": "Brief explanation of why these rules are logically inconsistent"
      }}
    ]
    """

    llm = get_llm_client()
    try:
        res = await llm.generate(
            prompt=prompt,
            system="You are a strict compliance verifier. Respond with raw JSON array only.",
            fast=True,
            max_tokens=1024,
        )
        
        # Self-healing clean-up
        clean_res = res.strip()
        if clean_res.startswith("```"):
            lines = clean_res.split("\n")
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                clean_res = "\n".join(lines[1:-1]).strip()
        
        try:
            conflicts = json.loads(clean_res)
            if isinstance(conflicts, list):
                return conflicts
        except json.JSONDecodeError as je:
            healed = await llm.self_heal_json(clean_res, str(je))
            if isinstance(healed, list):
                return healed
    except Exception as e:
        log.warning("policy_conflict_verification_failed", error=str(e))
        
    return []
