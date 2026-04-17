"""
Live integration tests — require running Docker services.

Verified against:
  - l1_redis on port 6380
  - l1_falkordb on port 6381 (FalkorDB, Redis-protocol)
  - l1_postgres on port 5432 (pgvector)

Graph node conventions (from seed_data.py):
  - Brand: property 'id' (not 'brand_id')
  - ReturnPolicy: property 'brand_id'; connected from Brand via HAS_POLICY
  - IssueType: property 'id'; steps stored as serialized 'steps' property on Resolution
  - ResolutionStep: stored as list inside Resolution.steps (not separate nodes)

Postgres schema (from init.sql):
  - knowledge_chunks: id, brand_id, content, embedding, ...
  - support_sessions: id, brand_id, channel, customer_id, ...
"""

import os
import sys
import asyncio
import socket
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def service_up(port: int) -> bool:
    try:
        s = socket.create_connection(("localhost", port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def falkordb_available() -> bool:
    from core.ontology.schema import verify_connectivity
    return verify_connectivity()


# ═══════════════════════════════════════════════════════════════════════════
# Redis (Tier 1 session store) — live tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not service_up(6380), reason="Redis not available on :6380")
class TestRedisSessionLive:

    @pytest.mark.asyncio
    async def test_redis_ping(self):
        from core.memory.session import get_redis
        r = get_redis()
        result = await r.ping()
        assert result is True

    @pytest.mark.asyncio
    async def test_session_create_and_read(self):
        from core.memory.session import create_session, get_session
        session_id = await create_session(brand_id="boohoo_uk", channel="chat", customer_id="live_cust_1")
        assert len(session_id) == 36
        data = await get_session(session_id)
        assert data is not None
        assert data["brand_id"] == "boohoo_uk"
        assert data["channel"] == "chat"
        assert data["customer_id"] == "live_cust_1"
        assert data["turn_count"] == 0
        assert data["is_resolved"] is False

    @pytest.mark.asyncio
    async def test_session_update_preserves_existing_fields(self):
        from core.memory.session import create_session, get_session, update_session
        session_id = await create_session(brand_id="plt_uk", channel="email")
        await update_session(session_id, {"turn_count": 3, "is_resolved": True})
        data = await get_session(session_id)
        assert data["turn_count"] == 3
        assert data["is_resolved"] is True
        assert data["brand_id"] == "plt_uk"  # original field preserved

    @pytest.mark.asyncio
    async def test_session_delete(self):
        from core.memory.session import create_session, get_session, SessionMemory
        session_id = await create_session(brand_id="boohoo_uk", channel="chat")
        mem = SessionMemory(session_id)
        await mem.delete()
        data = await get_session(session_id)
        assert data is None

    @pytest.mark.asyncio
    async def test_missing_session_returns_none(self):
        from core.memory.session import get_session
        data = await get_session("nonexistent-session-xyz-123")
        assert data is None

    @pytest.mark.asyncio
    async def test_multiple_sessions_independent(self):
        from core.memory.session import create_session, get_session
        ids = [await create_session(brand_id="boohoo_uk", channel="chat", customer_id=f"cust_{i}")
               for i in range(5)]
        assert len(set(ids)) == 5  # all unique UUIDs
        for sid in ids:
            assert await get_session(sid) is not None

    @pytest.mark.asyncio
    async def test_ttl_extend(self):
        from core.memory.session import create_session, SessionMemory
        session_id = await create_session(brand_id="boohoo_uk", channel="chat")
        mem = SessionMemory(session_id)
        await mem.extend_ttl()  # should not raise


# ═══════════════════════════════════════════════════════════════════════════
# FalkorDB (knowledge graph) — live tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not service_up(6381), reason="FalkorDB not available on :6381")
class TestFalkorDBLive:

    @pytest.fixture(autouse=True)
    def seed(self):
        from core.ontology.schema import get_graph_client, create_schema
        from core.ontology.seed_data import seed_all
        _, graph = get_graph_client()
        create_schema(graph)
        seed_all(graph)
        self.graph = graph

    def test_connectivity(self):
        from core.ontology.schema import verify_connectivity
        assert verify_connectivity() is True

    def test_13_brands_in_graph(self):
        result = self.graph.query("MATCH (b:Brand) RETURN count(b) AS cnt")
        assert result.result_set[0][0] == 13

    def test_brand_id_property_is_id(self):
        """Brand nodes use 'id' as the property name."""
        result = self.graph.query("MATCH (b:Brand {id: 'boohoo_uk'}) RETURN b.name, b.tier")
        assert len(result.result_set) == 1
        assert result.result_set[0][0] == "Boohoo"

    def test_return_policy_linked_to_brand(self):
        result = self.graph.query(
            "MATCH (b:Brand {id: 'boohoo_uk'})-[:HAS_POLICY]->(p:ReturnPolicy) "
            "RETURN p.return_window_days, p.return_cost_gbp"
        )
        assert len(result.result_set) >= 1
        assert result.result_set[0][0] == 28
        assert result.result_set[0][1] == 1.99

    def test_return_policy_brand_isolation(self):
        """PLT policies should only have plt_uk brand_id."""
        result = self.graph.query(
            "MATCH (p:ReturnPolicy {brand_id: 'plt_uk'}) RETURN p.brand_id"
        )
        assert len(result.result_set) >= 1
        for row in result.result_set:
            assert row[0] == "plt_uk"

    def test_karen_millen_free_returns(self):
        result = self.graph.query(
            "MATCH (p:ReturnPolicy {brand_id: 'karenmillen_uk'}) RETURN p.return_cost_gbp"
        )
        if result.result_set:
            assert result.result_set[0][0] == 0.0

    def test_11_issue_types_in_graph(self):
        result = self.graph.query("MATCH (i:IssueType) RETURN count(i) AS cnt")
        assert result.result_set[0][0] >= 11

    def test_wismo_resolution_exists_with_steps(self):
        """Steps are serialized into Resolution.steps (not separate ResolutionStep nodes)."""
        result = self.graph.query(
            "MATCH (i:IssueType {id: 'wismo'})-[:RESOLVED_BY]->(r:Resolution) "
            "RETURN r.id, r.steps"
        )
        assert len(result.result_set) >= 1
        assert result.result_set[0][0] == "wismo_track"
        steps = result.result_set[0][1]
        assert steps is not None and "order_lookup" in str(steps)

    def test_all_issue_types_have_resolutions(self):
        result = self.graph.query(
            "MATCH (i:IssueType)-[:RESOLVED_BY]->(r:Resolution) "
            "RETURN count(r) AS cnt"
        )
        assert result.result_set[0][0] >= 11

    def test_brand_retriever_get_brand_info(self):
        from core.knowledge.retriever import get_brand_info
        info = get_brand_info("boohoo_uk")
        assert info is not None
        assert "Boohoo" in str(info)

    def test_brand_retriever_return_policy_no_exception(self):
        from core.knowledge.retriever import get_return_policy
        from datetime import datetime, timezone
        policy = get_return_policy("boohoo_uk", datetime.now(timezone.utc))
        assert policy is None or isinstance(policy, dict)

    def test_has_policy_relationship_count(self):
        result = self.graph.query("MATCH ()-[:HAS_POLICY]->() RETURN count(*) AS cnt")
        # Should have ≥13 (one per brand at minimum)
        assert result.result_set[0][0] >= 13


# ═══════════════════════════════════════════════════════════════════════════
# Postgres (pgvector + app data) — live tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not service_up(5432), reason="Postgres not available on :5432")
class TestPostgresLive:

    @pytest.mark.asyncio
    async def test_postgres_connect(self):
        import asyncpg
        from config import settings
        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        version = await conn.fetchval("SELECT version()")
        await conn.close()
        assert "PostgreSQL" in version

    @pytest.mark.asyncio
    async def test_all_5_tables_exist(self):
        import asyncpg
        from config import settings
        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
        names = {r["tablename"] for r in tables}
        await conn.close()
        expected = {"knowledge_chunks", "support_sessions", "intent_label_queue",
                    "resolution_review_queue", "crawl_sources"}
        for t in expected:
            assert t in names, f"Missing table: {t}"

    @pytest.mark.asyncio
    async def test_knowledge_chunks_has_embedding_column(self):
        import asyncpg
        from config import settings
        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='knowledge_chunks'"
        )
        col_names = {r["column_name"] for r in cols}
        await conn.close()
        assert "embedding" in col_names
        assert "content" in col_names
        assert "brand_id" in col_names

    @pytest.mark.asyncio
    async def test_pgvector_extension_loaded(self):
        import asyncpg
        from config import settings
        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        ext = await conn.fetchval(
            "SELECT extname FROM pg_extension WHERE extname='vector'"
        )
        await conn.close()
        assert ext == "vector", "pgvector extension not installed"

    @pytest.mark.asyncio
    async def test_insert_and_query_support_session(self):
        import asyncpg
        from config import settings
        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "INSERT INTO support_sessions (brand_id, channel, customer_id, started_at) "
                "VALUES ($1, $2, $3, NOW())",
                "boohoo_uk", "chat", "live_test_customer",
            )
            row = await conn.fetchrow(
                "SELECT brand_id, channel FROM support_sessions "
                "WHERE customer_id='live_test_customer' ORDER BY started_at DESC LIMIT 1"
            )
            assert row["brand_id"] == "boohoo_uk"
            assert row["channel"] == "chat"
        finally:
            await conn.execute("DELETE FROM support_sessions WHERE customer_id='live_test_customer'")
            await conn.close()

    @pytest.mark.asyncio
    async def test_crawl_sources_schema(self):
        import asyncpg
        from config import settings
        dsn = settings.postgres_dsn.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='crawl_sources'"
        )
        col_names = {r["column_name"] for r in cols}
        await conn.close()
        assert "brand_id" in col_names
        assert "url" in col_names
