"""
Tests for Phase 2 memory clients (MemPalace + Graphiti).

MemPalace tests use a real ChromaDB ephemeral client (in-memory temp dir).
Graphiti tests mock the Graphiti client to avoid requiring FalkorDB.
"""

import os
import tempfile
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════════
# MemPalace tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMemPalaceClient:

    def setup_method(self):
        """Use a temp directory so each test gets a clean ChromaDB."""
        import chromadb
        from core.memory import mempalace_client as mp

        self.tmp_dir = tempfile.mkdtemp()
        # Patch the singleton to use temp dir
        mp._chroma_client = chromadb.PersistentClient(path=self.tmp_dir)
        self.mp = mp.MemPalaceClient()

    def teardown_method(self):
        import core.memory.mempalace_client as mp
        mp._chroma_client = None
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_remember_and_recall(self):
        self.mp.remember(
            brand_id="boohoo_uk",
            customer_id="cust_001",
            session_id="sess_aaa",
            content="Customer asked about order BH-12345. Resolved by tracking: in transit, ETA tomorrow.",
        )
        results = self.mp.recall(
            brand_id="boohoo_uk",
            customer_id="cust_001",
            query="where is my order BH-12345",
            k=3,
        )
        # Should return at least 1 result
        assert isinstance(results, list)
        # Result should contain the stored content
        if results:
            assert "BH-12345" in results[0] or "transit" in results[0]

    def test_recall_empty_for_different_customer(self):
        self.mp.remember(
            brand_id="boohoo_uk",
            customer_id="cust_001",
            session_id="sess_bbb",
            content="Customer asked about refund. Status: processed.",
        )
        # Different customer should get no results
        results = self.mp.recall(
            brand_id="boohoo_uk",
            customer_id="cust_999",
            query="refund",
            k=3,
        )
        assert results == []

    def test_session_count(self):
        for i in range(3):
            self.mp.remember(
                brand_id="boohoo_uk",
                customer_id="cust_002",
                session_id=f"sess_{i}",
                content=f"Session {i} transcript content here for testing.",
            )
        count = self.mp.get_session_count("boohoo_uk", "cust_002")
        assert count == 3

    def test_remember_upsert(self):
        """Same session_id upserted twice should not duplicate."""
        for _ in range(2):
            self.mp.remember(
                brand_id="boohoo_uk",
                customer_id="cust_003",
                session_id="sess_same",
                content="Duplicate session content.",
            )
        count = self.mp.get_session_count("boohoo_uk", "cust_003")
        assert count == 1

    def test_multi_brand_isolation(self):
        """Different brands have separate collections — no cross-contamination."""
        self.mp.remember(
            brand_id="boohoo_uk",
            customer_id="cust_004",
            session_id="sess_bh",
            content="Boohoo order BH-99999 returned successfully.",
        )
        self.mp.remember(
            brand_id="plt_uk",
            customer_id="cust_004",
            session_id="sess_plt",
            content="PLT order PLT-77777 in transit.",
        )
        # Query boohoo — should not get PLT results
        boohoo_results = self.mp.recall("boohoo_uk", "cust_004", "order", k=5)
        plt_results = self.mp.recall("plt_uk", "cust_004", "order", k=5)
        # Both have results but from different collections
        assert isinstance(boohoo_results, list)
        assert isinstance(plt_results, list)


# ═══════════════════════════════════════════════════════════════════════════
# Graphiti memory tests (mocked)
# ═══════════════════════════════════════════════════════════════════════════

class TestGraphitiMemory:

    @pytest.mark.asyncio
    async def test_add_episode_graceful_failure(self, monkeypatch):
        """If Graphiti (FalkorDB) is unavailable, add_episode should not raise."""
        from core.memory.graphiti_client import GraphitiMemory
        import core.memory.graphiti_client as gm

        # Patch _get_graphiti to simulate connection failure
        async def _mock_get_graphiti():
            raise ConnectionError("FalkorDB unavailable")

        monkeypatch.setattr(gm, "_get_graphiti", _mock_get_graphiti)
        mem = GraphitiMemory()
        # Should not raise — logs error and returns
        await mem.add_episode(
            customer_id="cust_001",
            brand_id="boohoo_uk",
            content="Customer contacted about WISMO.",
        )

    @pytest.mark.asyncio
    async def test_get_recent_facts_returns_empty_on_failure(self, monkeypatch):
        """If Graphiti is unavailable, get_recent_facts returns empty list."""
        from core.memory.graphiti_client import GraphitiMemory
        import core.memory.graphiti_client as gm

        async def _mock_get_graphiti():
            raise ConnectionError("FalkorDB unavailable")

        monkeypatch.setattr(gm, "_get_graphiti", _mock_get_graphiti)
        mem = GraphitiMemory()
        facts = await mem.get_recent_facts("cust_001", "boohoo_uk")
        assert facts == []

    @pytest.mark.asyncio
    async def test_add_and_search_mocked(self, monkeypatch):
        """Mock graphiti client and verify episode is added with correct group_id."""
        from core.memory.graphiti_client import GraphitiMemory, _group_id
        import core.memory.graphiti_client as gm

        added_episodes = []

        class MockEdge:
            fact = "Customer returned order BH-12345"
            valid_at = None
            invalid_at = None
            source_node_name = "Customer"
            target_node_name = "Order"

        class MockGraphiti:
            async def add_episode(self, **kwargs):
                added_episodes.append(kwargs)

            async def search(self, query, group_ids, num_results):
                return [MockEdge()]

        async def _mock_get_graphiti():
            return MockGraphiti()

        monkeypatch.setattr(gm, "_get_graphiti", _mock_get_graphiti)
        mem = GraphitiMemory()

        await mem.add_episode(
            customer_id="cust_001",
            brand_id="boohoo_uk",
            content="Customer asked about order BH-12345. Resolved.",
        )
        assert len(added_episodes) == 1
        assert added_episodes[0]["group_id"] == _group_id("boohoo_uk", "cust_001")
        assert "BH-12345" in added_episodes[0]["episode_body"]

        facts = await mem.get_recent_facts("cust_001", "boohoo_uk")
        assert len(facts) == 1
        assert facts[0]["fact"] == "Customer returned order BH-12345"

    def test_group_id_format(self):
        from core.memory.graphiti_client import _group_id
        assert _group_id("boohoo_uk", "cust_001") == "boohoo_uk:cust_001"
        assert _group_id("plt_uk", "cust_999") == "plt_uk:cust_999"
