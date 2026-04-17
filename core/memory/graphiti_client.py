"""
Tier 3 Memory: Graphiti temporal knowledge graph.

Uses graphiti-core with FalkorDB backend.
Each customer's facts are namespaced under group_id = "brand:customer_id"
so multi-brand data stays isolated.

Facts stored per interaction:
  - Contact history (what the customer called/chatted about)
  - Order outcomes (returns, refunds, cancellations)
  - Preferences (size, brand affinity, preferred channel)
  - Escalations (reason, agent, outcome)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from config import settings

log = structlog.get_logger(__name__)


def _group_id(brand_id: str, customer_id: str) -> str:
    """Deterministic group_id for brand×customer isolation."""
    return f"{brand_id}:{customer_id}"


def _build_graphiti():
    """Build a configured Graphiti instance using project settings."""
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.llm_client.config import LLMConfig

    driver = FalkorDriver(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password or None,
        database=settings.graphiti_graph_name,
    )

    p = settings.llm_provider.lower()
    if p == "anthropic":
        from graphiti_core.llm_client.anthropic_client import AnthropicClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        llm_client = AnthropicClient(
            config=LLMConfig(api_key=settings.anthropic_api_key, model=settings.primary_model)
        )
        # Anthropic has no embeddings — use OpenAI if key exists, else None (graphiti stubs it)
        embedder = None
        if settings.openai_api_key:
            embedder = OpenAIEmbedder(
                config=OpenAIEmbedderConfig(api_key=settings.openai_api_key)
            )
    elif p == "azure":
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        import openai
        azure_client = openai.AsyncAzureOpenAI(
            api_key=settings.azure_openai_api_key,
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
        )
        llm_client = OpenAIClient(
            config=LLMConfig(model=settings.azure_primary_deployment),
            client=azure_client,
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(model=settings.azure_embedding_deployment),
            client=azure_client,
        )
    else:  # openai
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        llm_client = OpenAIClient(
            config=LLMConfig(api_key=settings.openai_api_key, model=settings.openai_primary_model)
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(api_key=settings.openai_api_key)
        )

    kwargs: dict[str, Any] = {"graph_driver": driver, "llm_client": llm_client}
    if embedder is not None:
        kwargs["embedder"] = embedder

    return Graphiti(**kwargs)


# Lazy singleton — initialised once on first use
_graphiti = None
_init_lock = asyncio.Lock()


async def _get_graphiti():
    global _graphiti
    if _graphiti is None:
        async with _init_lock:
            if _graphiti is None:
                _graphiti = _build_graphiti()
                try:
                    await _graphiti.build_indices_and_constraints()
                    log.info("graphiti_ready", host=settings.falkordb_host)
                except Exception as exc:
                    log.warning("graphiti_index_skip", error=str(exc))
    return _graphiti


# ═══════════════════════════════════════════════════════════════════════════


class GraphitiMemory:
    """
    Temporal customer memory backed by Graphiti + FalkorDB.

    add_episode() — ingest a conversation turn or outcome fact
    get_recent_facts() — retrieve edges relevant to a customer query
    search() — free-form semantic search over a customer's graph
    """

    async def add_episode(
        self,
        customer_id: str,
        brand_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        episode_name: str | None = None,
    ) -> None:
        """
        Persist a conversation episode or fact to the temporal KG.

        content — natural-language description of what happened
                  e.g. "Customer contacted about WISMO for order BH-12345.
                        Shipment found in_transit, ETA 2 days. Resolved."
        """
        from graphiti_core.nodes import EpisodeType

        name = episode_name or f"support:{brand_id}:{customer_id}:{datetime.now(timezone.utc).isoformat()}"
        group = _group_id(brand_id, customer_id)

        meta_str = ""
        if metadata:
            meta_str = " | " + " | ".join(f"{k}={v}" for k, v in metadata.items())

        try:
            g = await _get_graphiti()
            await g.add_episode(
                name=name,
                episode_body=content + meta_str,
                source_description=f"L1 support session for brand={brand_id}",
                reference_time=datetime.now(timezone.utc),
                source=EpisodeType.text,
                group_id=group,
            )
            log.debug("graphiti_episode_added", customer_id=customer_id, brand_id=brand_id)
        except Exception as exc:
            log.error("graphiti_add_episode_failed", error=str(exc), customer_id=customer_id)

    async def get_recent_facts(
        self,
        customer_id: str,
        brand_id: str,
        query: str = "",
        days: int = 30,
        num_results: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Retrieve temporal facts about a customer.

        Returns list of edge dicts: {fact, valid_at, invalid_at, source_node, target_node}
        """
        group = _group_id(brand_id, customer_id)
        search_query = query or f"customer {customer_id} contact history"

        try:
            g = await _get_graphiti()
            edges = await g.search(
                query=search_query,
                group_ids=[group],
                num_results=num_results,
            )
            facts = []
            for edge in edges:
                facts.append({
                    "fact": edge.fact,
                    "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                    "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
                    "source": getattr(edge, "source_node_name", ""),
                    "target": getattr(edge, "target_node_name", ""),
                })
            log.debug("graphiti_facts_retrieved", customer_id=customer_id, count=len(facts))
            return facts
        except Exception as exc:
            log.error("graphiti_search_failed", error=str(exc), customer_id=customer_id)
            return []

    async def search(
        self,
        customer_id: str,
        brand_id: str,
        query: str,
        num_results: int = 5,
    ) -> list[str]:
        """Free-form search — returns list of fact strings for context injection."""
        facts = await self.get_recent_facts(
            customer_id=customer_id,
            brand_id=brand_id,
            query=query,
            num_results=num_results,
        )
        return [f["fact"] for f in facts if f.get("fact")]


# Module-level singleton
graphiti_memory = GraphitiMemory()
