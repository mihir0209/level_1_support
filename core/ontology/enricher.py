"""Self-enriching Knowledge Graph Loop engine."""

from __future__ import annotations

import json
import uuid
import structlog
from core.memory.session import get_redis
from core.ontology.schema import get_graph_client

log = structlog.get_logger(__name__)


async def extract_and_propose_facts(session_id: str, brand_id: str, messages: list[dict]) -> dict | None:
    """
    Parse a conversation transcript to extract potential new factual assertions.
    Generates Cypher query proposals to update FalkorDB.
    """
    if not messages:
        return None

    # Format dialogue context
    dialogue = "\n".join([f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages])

    prompt = f"""
    Analyze the following resolved customer support dialogue. Identify any new factual assertions, product issues, or courier patterns that could be written back to our knowledge graph.

    DIALOGUE:
    {dialogue}

    CONSTRAINTS:
    - We look for general insights, e.g. a specific product SKU runs small, or a shipping carrier has delays.
    - Do NOT extract transient customer details (like names, personal addresses, or credit cards).
    - Propose a valid Cypher CREATE statement to insert this fact.

    Respond with ONLY a JSON object containing the extraction details. If no significant new fact is discovered, return null.
    Do NOT include markdown wrapping or conversational chatter.

    JSON Scheme:
    {{
      "explanation": "Factual description of the discovered assertion",
      "cypher_query": "CREATE (i:Incident {{name: 'Evri delays in London', type: 'shipping_delay'}}) RETURN i"
    }}
    """

    from core.orchestration.llm import get_llm_client
    llm = get_llm_client()
    try:
        res = await llm.generate(
            prompt=prompt,
            system="You are a precise ontological graph builder. Respond with ONLY JSON object or null.",
            fast=True,
            max_tokens=512,
        )
        
        clean_res = res.strip()
        if clean_res.lower() == "null" or not clean_res:
            return None
            
        if clean_res.startswith("```"):
            lines = clean_res.split("\n")
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                clean_res = "\n".join(lines[1:-1]).strip()

        try:
            fact = json.loads(clean_res)
            if isinstance(fact, dict) and "cypher_query" in fact:
                fid = str(uuid.uuid4())
                fact["id"] = fid
                fact["session_id"] = session_id
                fact["brand_id"] = brand_id
                
                # Save to pending queue in Redis
                r = get_redis()
                await r.hset("graph:enrichments", fid, json.dumps(fact))
                log.info("graph_enrichment_fact_proposed", fact_id=fid, session_id=session_id)
                return fact
        except json.JSONDecodeError as je:
            healed = await llm.self_heal_json(clean_res, str(je))
            if isinstance(healed, dict) and "cypher_query" in healed:
                fid = str(uuid.uuid4())
                healed["id"] = fid
                healed["session_id"] = session_id
                healed["brand_id"] = brand_id
                r = get_redis()
                await r.hset("graph:enrichments", fid, json.dumps(healed))
                return healed
    except Exception as e:
        log.warning("graph_enrichment_fact_extraction_failed", error=str(e))

    return None


async def approve_enrichment(enrichment_id: str) -> bool:
    """Execute Cypher write and commit the fact to FalkorDB."""
    try:
        r = get_redis()
        raw = await r.hget("graph:enrichments", enrichment_id)
        if not raw:
            return False

        fact = json.loads(raw)
        cypher = fact.get("cypher_query")
        
        # Execute query in FalkorDB
        if cypher:
            _, graph = get_graph_client()
            graph.query(cypher)
            
        # Delete from pending queue
        await r.hdel("graph:enrichments", enrichment_id)
        log.info("graph_enrichment_approved", fact_id=enrichment_id)
        return True
    except Exception as e:
        log.warning("graph_enrichment_approval_failed", fact_id=enrichment_id, error=str(e))
        raise


async def reject_enrichment(enrichment_id: str) -> None:
    """Discard and archive the proposed fact from pending queue."""
    try:
        r = get_redis()
        await r.hdel("graph:enrichments", enrichment_id)
        log.info("graph_enrichment_rejected", fact_id=enrichment_id)
    except Exception as e:
        log.warning("graph_enrichment_rejection_failed", fact_id=enrichment_id, error=str(e))
        raise


async def get_pending_enrichments() -> list[dict]:
    """Retrieve list of all pending discovered facts."""
    enrichments = []
    try:
        r = get_redis()
        raw = await r.hgetall("graph:enrichments")
        for fid, val in raw.items():
            enrichments.append(json.loads(val))
    except Exception as e:
        log.warning("graph_enrichments_get_failed", error=str(e))
    return enrichments
