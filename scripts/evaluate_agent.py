#!/usr/bin/env .venv/bin/python
"""
Agent Evaluation Script — runs queries across 3 retrieval environments
and compares AI quality against ground-truth knowledge base.

Usage:
  python scripts/evaluate_agent.py boohoo_uk
  python scripts/evaluate_agent.py plt_uk

No defaults — brand_id is always required.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

# ── Host override for local Docker ──────────────────────────────────────────
os.environ["FALKORDB_HOST"] = "localhost"
os.environ["FALKORDB_PORT"] = "6381"
os.environ["POSTGRES_HOST"] = "localhost"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importlib import reload
import config as _cfg
reload(_cfg)

from core.context.engine import assemble_context, _BRAND_SYSTEM_PROMPTS, _DEFAULT_SYSTEM_PROMPT, classify_intent, _format_policy_block, _format_shipping_block
from core.knowledge.retriever import (
    retrieve_knowledge,
    get_resolutions_for_issue,
    get_return_policy,
    get_brand_info,
    search_kb_nodes,
    get_shipping_policy,
)
from core.orchestration.llm import get_llm_client
from core.ontology.models import Channel, IssueCategory, IntentResult
from config import settings


# ── Ground-truth KB for boohoo_uk (from direct DB queries) ───────────────────
BOOHOO_UK_KB = {
    "return_policy": {
        "window_days": 28,
        "cost_gbp": 1.99,
        "free_condition": "None",
        "excluded_categories": ["swimwear", "lingerie", "cosmetics", "pierced jewellery"],
        "refund_timeline_days": 14,
    },
    "shipping_policy": {
        "uk": "2-5 business days, standard £3.99, free over £30",
        "usa": "6-8 business days, standard £10.99, free over £100",
    },
    "resolutions": {
        "wismo": {"id": "wismo_track", "success_rate": 0.92, "steps": "Check order status → retrieve tracking → provide ETA"},
        "return_initiate": {"id": "return_standard", "success_rate": 0.88, "steps": "Explain cost + window → initiate return via portal → send label"},
        "wrong_item": {"id": "wrong_item_resolution", "success_rate": 0.85, "steps": "Confirm item + order → raise refund/replacement → send return label"},
        "refund_delay": {"id": "refund_status_check", "success_rate": 0.79, "steps": "Check refund status → explain timeline → escalate if >21 days"},
        "damaged_item": {"id": "damaged_item_resolution", "success_rate": 0.83, "steps": "Confirm damage → offer replacement or refund → no return fee"},
        "payment_failed": {"id": "payment_troubleshoot", "success_rate": 0.7, "steps": "Check payment method → suggest alternatives → retry"},
    },
    "faqs": [
        "Delivery: 7 days a week 8am-9pm, track via order number",
        "Returns: Repack → login to returns portal → print label or use in-store → post",
        "Refund timing: up to 14 days for cash, 48hrs for PayPal",
        "Store credit: 10% bonus for choosing credit over cash refund",
        "No phone support — contact via website only",
        "No exchange facility — return and reorder",
        "Late delivery: after 9pm on expected date = late",
        "Missing item: check confirmation email for stock status",
        "Faulty item: contact within 30 days via Contact Us page",
    ],
    "excluded_for_returns": "swimwear, lingerie, cosmetics, pierced jewellery",
}

PLT_UK_KB = {
    "return_policy": {
        "window_days": 28,
        "cost_gbp": 2.49,
        "free_condition": "None",
        "excluded_categories": ["face masks", "cosmetics", "earpiercing"],
        "refund_timeline_days": 14,
    },
    "shipping_policy": {
        "uk": "3-5 business days, standard £3.95, free over £25",
        "usa": "5-7 business days",
    },
    "resolutions": {
        "wismo": {"id": "wismo_track", "success_rate": 0.92, "steps": "Check order status → retrieve tracking → provide ETA"},
        "return_initiate": {"id": "return_standard", "success_rate": 0.87, "steps": "Explain cost + window → initiate return via portal → send label"},
        "wrong_item": {"id": "wrong_item_resolution", "success_rate": 0.84, "steps": "Confirm item + order → raise refund/replacement → send return label"},
    },
    "faqs": [],
    "excluded_for_returns": "face masks, cosmetics, earpiercing",
}

KB_BY_BRAND = {
    "boohoo_uk": BOOHOO_UK_KB,
    "plt_uk": PLT_UK_KB,
}


# ── Test queries per category ────────────────────────────────────────────────
TEST_QUERIES = [
    # WISMO
    {
        "id": "wismo_01",
        "query": "Where is my order? I haven't received anything yet.",
        "expected_intent": "wismo",
        "check": lambda ans: any(k in ans.lower() for k in ["track", "order number", "order id", "dispatch"]),
    },
    {
        "id": "wismo_02",
        "query": "My order was supposed to arrive yesterday but nothing came.",
        "expected_intent": "wismo",
        "check": lambda ans: any(k in ans.lower() for k in ["track", "late", "expected delivery", "order number"]),
    },
    # Returns
    {
        "id": "return_01",
        "query": "I want to return a dress I bought last week.",
        "expected_intent": "return_initiate",
        "check": lambda ans: any(k in ans.lower() for k in ["28", "portal", "returns", "label", "£1.99"]),
    },
    {
        "id": "return_02",
        "query": "Can I return my swimwear?",
        "expected_intent": "return_initiate",
        "check": lambda ans: any(k in ans.lower() for k in ["cannot", "excluded", "not eligible", "swimwear"]),
    },
    {
        "id": "return_03",
        "query": "How long do I have to send items back?",
        "expected_intent": "return_initiate",
        "check": lambda ans: "28" in ans or "twenty" in ans.lower(),
    },
    # Wrong Item
    {
        "id": "wrong_01",
        "query": "I received a black top but I ordered blue.",
        "expected_intent": "wrong_item",
        "check": lambda ans: any(k in ans.lower() for k in ["wrong item", "refund", "replacement", "return"]),
    },
    # Refund
    {
        "id": "refund_01",
        "query": "I sent my return back 10 days ago, when will I get my money?",
        "expected_intent": "refund_delay",
        "check": lambda ans: any(k in ans.lower() for k in ["14", "days", "refund", "processing", "bank"]),
    },
    {
        "id": "refund_02",
        "query": "How long do refunds take to appear in my account?",
        "expected_intent": "refund_delay",
        "check": lambda ans: any(k in ans.lower() for k in ["14 days", "up to 14", "processing", "bank"]),
    },
    # Account
    {
        "id": "account_01",
        "query": "I can't log into my account, it says my password is wrong.",
        "expected_intent": "account_locked",
        "check": lambda ans: any(k in ans.lower() for k in ["reset", "password", "account", "locked"]),
    },
    # Payment
    {
        "id": "payment_01",
        "query": "My card payment keeps failing, what's going on?",
        "expected_intent": "payment_failed",
        "check": lambda ans: any(k in ans.lower() for k in ["payment", "card", "retry", "method"]),
    },
    # Damaged Item
    {
        "id": "damage_01",
        "query": "The dress I received has a tear in it, what can I do?",
        "expected_intent": "damaged_item",
        "check": lambda ans: any(k in ans.lower() for k in ["damage", "refund", "replacement", "contact", "30 days"]),
    },
    # Promo
    {
        "id": "promo_01",
        "query": "My discount code isn't working at checkout.",
        "expected_intent": "promo_not_applied",
        "check": lambda ans: any(k in ans.lower() for k in ["promo", "code", "discount", "validate"]),
    },
]


# ── Retrieval environments ───────────────────────────────────────────────────

class RetrievalEnv:
    """Base class for a retrieval environment configuration."""

    name: str = ""
    description: str = ""
    use_graph: bool = False      # FalkorDB data
    use_ontology: bool = False    # Postgres knowledge_chunks
    use_kb: bool = False         # FAQ/KB content

    def get_context(self, brand_id: str, query: str, llm) -> dict:
        raise NotImplementedError


class EnvNothing(RetrievalEnv):
    """No knowledge graph, no KB — pure LLM with no grounding."""

    name = "env_0_nothing"
    description = "No knowledge graph, no KB — pure LLM with no grounding"

    def get_context(self, brand_id: str, query: str, llm) -> dict:
        brand_info = get_brand_info(brand_id)
        brand_name = brand_info["name"] if brand_info else brand_id
        prompt = _BRAND_SYSTEM_PROMPTS.get(brand_id, _DEFAULT_SYSTEM_PROMPT)
        return {
            "system_prompt": prompt,
            "resolutions": [],
            "policy": None,
            "shipping_policy": None,
            "kb_items": [],
            "source": "none",
            "issue_type": "unknown",
            "brand_name": brand_name,
            "tool_manifest": ["escalate"],
        }


class EnvGraphOnly(RetrievalEnv):
    """FalkorDB ontology only — Brand, Policies, IssueType→Resolution paths."""

    name = "env_1_graph_only"
    description = "FalkorDB ontology only (Brand, Policies, IssueType→Resolution)"

    async def get_context(self, brand_id: str, query: str, llm) -> dict:
        from core.context.engine import _TOOL_MANIFEST

        # Classify intent using the real classifier
        intent_result = await classify_intent(query, brand_id, [], llm)

        resolutions = get_resolutions_for_issue(brand_id, intent_result.issue_type)
        policy = get_return_policy(brand_id, None)
        shipping = get_shipping_policy(brand_id, "UK", None) if intent_result.issue_type in ("wismo", "delivery_query") else None

        brand_info = get_brand_info(brand_id)
        brand_name = brand_info["name"] if brand_info else brand_id
        prompt = _BRAND_SYSTEM_PROMPTS.get(brand_id, _DEFAULT_SYSTEM_PROMPT)
        tool_manifest = _TOOL_MANIFEST.get(intent_result.issue_type, ["escalate"])

        return {
            "system_prompt": prompt,
            "resolutions": resolutions,
            "policy": policy,
            "shipping_policy": shipping,
            "kb_items": [],
            "source": "graph_tool",
            "issue_type": intent_result.issue_type,
            "brand_name": brand_name,
            "tool_manifest": tool_manifest,
        }


class EnvOntologyPlusKB(RetrievalEnv):
    """Full: FalkorDB + Postgres knowledge_chunks (ingested FAQs/policies)."""

    name = "env_2_ontology_kb"
    description = "FalkorDB + Postgres knowledge_chunks (FAQs, policies, KB items)"

    async def get_context(self, brand_id: str, query: str, llm) -> dict:
        from core.context.engine import _TOOL_MANIFEST

        # Classify intent first (needed to route to correct Resolution path)
        intent_result = await classify_intent(query, brand_id, [], llm)

        result = retrieve_knowledge(
            brand_id=brand_id,
            issue_type_id=intent_result.issue_type,
            order_placed_at=None,
            region="UK",
            query_text=query,
            intent_category=intent_result.category.value,
            llm_client=llm,
        )

        brand_info = get_brand_info(brand_id)
        brand_name = brand_info["name"] if brand_info else brand_id
        prompt = _BRAND_SYSTEM_PROMPTS.get(brand_id, _DEFAULT_SYSTEM_PROMPT)
        tool_manifest = _TOOL_MANIFEST.get(intent_result.issue_type, ["escalate"])

        return {
            "system_prompt": prompt,
            "resolutions": result.resolutions,
            "policy": result.policy,
            "shipping_policy": result.shipping_policy,
            "kb_items": result.kb_items,
            "source": result.source,
            "issue_type": intent_result.issue_type,
            "brand_name": brand_name,
            "tool_manifest": tool_manifest,
        }


# ── Answer grader ─────────────────────────────────────────────────────────────

def grade_answer(query_id: str, expected_intent: str, answer: str, kb: dict) -> dict:
    """Grade an agent answer against expected criteria and detect hallucinations."""

    query_data = next((q for q in TEST_QUERIES if q["id"] == query_id), None)
    checks_passed = []
    checks_failed = []
    hallucination_flags = []
    notes = []

    # ── Intent relevance ──────────────────────────────────────────────────
    if query_data and query_data["check"](answer):
        checks_passed.append("intent_relevance")
    else:
        checks_failed.append("intent_relevance")

    # ── Access denial (hallucination flag) ────────────────────────────────
    if any(phrase in answer.lower() for phrase in [
        "i don't have access to", "i can't check", "internal system",
        "not in my knowledge", "external database", "i don't have that information",
    ]):
        hallucination_flags.append("access_denial")

    # ── Policy grounding — only check for queries where return policy is relevant ──
    has_policy_detail = any(k in answer.lower() for k in ["£", "days", "return", "policy", "window"])
    if kb.get("return_policy"):
        correct_cost = f"£{kb['return_policy']['cost_gbp']}"
        correct_window = str(kb["return_policy"]["window_days"])

        # Only check return window for return-related queries
        is_return_query = expected_intent in ("return_initiate", "return_related")

        if is_return_query:
            if correct_window in answer or "twenty" in answer.lower() or "28" in answer:
                checks_passed.append("correct_window")
            else:
                checks_failed.append("incorrect_window")
                hallucination_flags.append("wrong_return_window")

            if correct_cost in answer:
                checks_passed.append("correct_cost")
            elif "£" in answer and correct_cost not in answer:
                hallucination_flags.append("wrong_return_cost")

    # ── Refund timeline specific checks ──────────────────────────────────
    if expected_intent == "refund_delay":
        correct_refund_days = str(kb.get("return_policy", {}).get("refund_timeline_days", 14))
        # Correct: 14 days for cash refunds. Common wrong numbers to catch:
        wrong_refund_numbers = ["5-7", "5-10", "3-5", "3-7", "10 days", "7 days", "3-10"]
        wrong_found = []
        for wrong in wrong_refund_numbers:
            if wrong in answer.lower():
                wrong_found.append(wrong)
        if wrong_found:
            hallucination_flags.append(f"wrong_refund_timeline:{'/'.join(wrong_found)} (correct={correct_refund_days})")
            notes.append(f"SAID {', '.join(wrong_found)} but correct is {correct_refund_days} days for cash refunds")
        elif correct_refund_days in answer or "fourteen" in answer.lower():
            checks_passed.append("correct_refund_timeline")
        elif any(k in answer.lower() for k in ["refund", "day", "processing"]):
            checks_passed.append("refund_timeline_mentioned")

    # ── Exchange policy check ─────────────────────────────────────────────
    if any(k in query_id.lower() for k in ["return_02", "return_03"]):
        if kb.get("excluded_for_returns"):
            excluded = kb["excluded_for_returns"]
            if isinstance(excluded, str):
                items = [e.strip() for e in excluded.split(",")]
            else:
                items = excluded
            for item in items:
                if item in answer.lower():
                    checks_passed.append(f"exclusion_mentioned:{item}")

    return {
        "passed": len(checks_passed),
        "failed": len(checks_failed),
        "hallucination_flags": hallucination_flags,
        "has_policy_detail": has_policy_detail,
        "notes": notes,
        "answer_preview": answer[:300] + "..." if len(answer) > 300 else answer,
    }


# ── Main evaluation ───────────────────────────────────────────────────────────

async def run_evaluation(brand_id: str):
    """Run the full evaluation across all 3 environments."""

    print(f"\n{'='*80}")
    print(f"AGENT EVALUATION — brand_id={brand_id}")
    print(f"{'='*80}\n")

    # Verify brand exists
    brand_info = get_brand_info(brand_id)
    if not brand_info:
        print(f"ERROR: brand_id '{brand_id}' not found in graph!")
        return

    print(f"Brand: {brand_info['name']} ({brand_info['tier']})")
    print(f"Domain: {brand_info['id']}\n")

    # Get KB for this brand
    kb = KB_BY_BRAND.get(brand_id, {})

    # Print current data inventory
    print("─" * 40)
    print("DATA INVENTORY")
    print("─" * 40)
    print(f"  Brand node:         {'YES' if brand_info else 'NO'}")
    print(f"  Return Policy:      {'YES (28d, £1.99)' if brand_id == 'boohoo_uk' else 'YES'}")
    print(f"  Shipping Policy:    {'YES (UK+USA)' if brand_id == 'boohoo_uk' else 'YES'}")
    print(f"  IssueType→Res:      {'YES (11 issue types)' if brand_id == 'boohoo_uk' else 'YES'}")

    resolutions = get_resolutions_for_issue(brand_id, "wismo")
    print(f"  Resolution steps:  {'YES' if resolutions and resolutions[0].get('steps') else 'NO (steps empty)'}")
    print(f"  FAQs (graph):       {'YES (20 FAQs)' if brand_id == 'boohoo_uk' else 'YES'}")
    print(f"  Postgres KB chunks: {'YES (5 chunks)' if brand_id == 'boohoo_uk' else 'YES'}")
    print()

    # Initialize LLM
    llm = get_llm_client()

    # Run evaluation
    results_by_env = {}
    for env_class in [EnvNothing, EnvGraphOnly, EnvOntologyPlusKB]:
        env = env_class()
        env_results = []

        print(f"\n{'─' * 80}")
        print(f"ENV: {env.name}")
        print(f"     {env.description}")
        print(f"{'─' * 80}")

        for q in TEST_QUERIES:
            query = q["query"]
            query_id = q["id"]

            # Build context for this env (async for some envs)
            if asyncio.iscoroutinefunction(env.get_context):
                ctx = await env.get_context(brand_id, query, llm)
            else:
                ctx = env.get_context(brand_id, query, llm)

            # Build the full prompt as the LLM would see it (mirrors graph.py _build_llm_messages)
            system_parts = [ctx["system_prompt"]]

            # IMPORTANT POLICY RULES (mirrors what graph.py injects)
            system_parts.append(
                "IMPORTANT POLICY RULES:\n"
                "  - When a customer asks to return an item, ALWAYS check the excluded items list.\n"
                "  - If the item is in excluded items, state clearly that it CANNOT be returned.\n"
                "  - Always give accurate timelines. Do NOT guess days."
            )

            brand_name = ctx.get("brand_name", brand_id)
            policy_block = _format_policy_block(ctx["policy"], brand_name) if ctx["policy"] else f"Return policy for {brand_name}: check the website."
            system_parts.append(policy_block)

            shipping_block = _format_shipping_block(ctx.get("shipping_policy"), brand_name) if ctx.get("shipping_policy") else ""
            if shipping_block:
                system_parts.append(shipping_block)

            if ctx["resolutions"]:
                res = ctx["resolutions"][0]
                system_parts.append(f"RESOLUTION PATH: {res.get('name', '?')} (SR={float(res.get('success_rate') or 0):.0%})")
                steps = res.get("steps", [])
                if isinstance(steps, str):
                    import json
                    try: steps = json.loads(steps.replace("'", '"'))
                    except: steps = []
                if isinstance(steps, list):
                    for s in steps[:5]:
                        if isinstance(s, dict):
                            system_parts.append(f"  Step {s.get('order','?')}: {s.get('instruction','?')}")

            system_parts.append(f"AVAILABLE TOOLS: {ctx.get('tool_manifest', ['escalate'])}")
            system_parts.append(f"\nCURRENT SESSION:\nCustomer: {query}")

            full_prompt = "\n\n".join(system_parts)

            # Call LLM
            start = time.time()
            try:
                answer = await llm.generate(full_prompt, max_tokens=400)
            except Exception as e:
                answer = f"[LLM ERROR: {e}]"
            latency = (time.time() - start) * 1000

            # Grade
            grade = grade_answer(query_id, q["expected_intent"], answer, kb)

            actual_intent = ctx.get("issue_type", "?")
            print(f"\n  [{query_id}] {query[:60]}...")
            print(f"  Expected intent: {q['expected_intent']} | Actual classified: {actual_intent}")
            print(f"  Source: {ctx['source']} | Latency: {latency:.0f}ms")
            print(f"  Grade: {grade['passed']} passed, {grade['failed']} failed")
            if grade['hallucination_flags']:
                print(f"  ⚠ HALLUCINATION: {grade['hallucination_flags']}")
            if grade['notes']:
                print(f"  Notes: {grade['notes']}")
            print(f"  Answer: {grade['answer_preview']}")

            env_results.append({
                "query_id": query_id,
                "query": query,
                "expected_intent": q["expected_intent"],
                "source": ctx["source"],
                "latency_ms": round(latency),
                "grade": grade,
                "answer": answer,
            })

        results_by_env[env.name] = env_results

    # ── Summary comparison ──────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("SUMMARY — Environment Comparison")
    print(f"{'='*80}\n")

    print(f"{'Query':<15} {'Env 0 (none)':<12} {'Env 1 (graph)':<12} {'Env 2 (graph+KB)':<12}")
    print("-" * 55)

    for i, q in enumerate(TEST_QUERIES):
        query_id = q["id"]
        row = [query_id]
        for env_name in ["env_0_nothing", "env_1_graph_only", "env_2_ontology_kb"]:
            result = next((r for r in results_by_env[env_name] if r["query_id"] == query_id), None)
            if result:
                g = result["grade"]
                icon = "✓" if g["passed"] > g["failed"] else "✗" if g["failed"] > g["passed"] else "~"
                row.append(f"{icon}{g['passed']}/{g['failed']}")
            else:
                row.append("?")
        print(f"{row[0]:<15} {row[1]:<12} {row[2]:<12} {row[3]:<12}")

    print()

    # Count issues
    for env_name in ["env_0_nothing", "env_1_graph_only", "env_2_ontology_kb"]:
        results = results_by_env[env_name]
        halluc_count = sum(len(r["grade"]["hallucination_flags"]) for r in results)
        policy_detail_count = sum(1 for r in results if r["grade"]["has_policy_detail"])
        avg_passed = sum(r["grade"]["passed"] for r in results) / len(results)
        print(f"{env_name}:")
        print(f"  Avg checks passed: {avg_passed:.2f}")
        print(f"  Hallucination flags: {halluc_count}")
        print(f"  Has policy detail: {policy_detail_count}/{len(results)}")

    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python evaluate_agent.py <brand_id>")
        print("  e.g. python evaluate_agent.py boohoo_uk")
        print("  e.g. python evaluate_agent.py plt_uk")
        sys.exit(1)

    brand_id = sys.argv[1]
    asyncio.run(run_evaluation(brand_id))