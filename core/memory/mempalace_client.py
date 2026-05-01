"""
Tier 2 Memory: MemPalace — 30-day verbatim session storage.

Implemented on top of ChromaDB (persistent, local) with a hierarchical
namespace that mirrors the MemPalace Wing→Room→Drawer model:

  Wing   = brand_id          (one collection per brand)
  Room   = customer_id       (metadata filter)
  Drawer = session_id        (metadata filter)

Collection name: mempalace_{brand_id}

Each document stored = one compressed session transcript.
Retrieval uses semantic similarity (ChromaDB cosine) with metadata filters.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Use absolute path rooted at project root — works in both Docker and local dev.
# Docker mounts ./data/mempalace → /app/data/mempalace, matching this path.
_DATA_DIR = str(Path(__file__).parent.parent.parent / "data" / "mempalace")

_RETENTION_DAYS = 30


def _collection_name(brand_id: str) -> str:
    # ChromaDB collection names: alphanumeric + dash/underscore, 3-63 chars
    safe = brand_id.replace(".", "_").replace("-", "_")[:50]
    return f"mp_{safe}"


def _doc_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


# ── ChromaDB client (lazy singleton) ─────────────────────────────────────────

_chroma_client = None


def _get_chroma():
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        os.makedirs(_DATA_DIR, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=_DATA_DIR)
        log.info("mempalace_chroma_ready", path=_DATA_DIR)
    return _chroma_client


def _get_collection(brand_id: str):
    client = _get_chroma()
    name = _collection_name(brand_id)
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


# ═══════════════════════════════════════════════════════════════════════════


class MemPalaceClient:
    """
    30-day verbatim session memory using ChromaDB.

    remember() — store a session transcript
    recall()   — retrieve top-k relevant past sessions for a customer
    forget_expired() — purge sessions older than RETENTION_DAYS
    """

    def remember(
        self,
        brand_id: str,
        customer_id: str | None,
        session_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Store a session transcript in the Wing (brand) / Room (customer) / Drawer (session).
        Synchronous — ChromaDB operations are local and fast.
        """
        col = _get_collection(brand_id)
        doc_id = _doc_id(session_id)
        meta: dict[str, Any] = {
            "customer_id": customer_id or "anonymous",
            "session_id": session_id,
            "brand_id": brand_id,
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "expire_at": (datetime.now(timezone.utc) + timedelta(days=_RETENTION_DAYS)).isoformat(),
        }
        if metadata:
            meta.update(metadata)

        try:
            col.upsert(
                ids=[doc_id],
                documents=[content],
                metadatas=[meta],
            )
            log.debug("mempalace_stored", session_id=session_id, customer_id=customer_id)
        except Exception as exc:
            log.error("mempalace_store_failed", error=str(exc), session_id=session_id)

    def recall(
        self,
        brand_id: str,
        customer_id: str | None,
        query: str,
        k: int = 3,
    ) -> list[str]:
        """
        Retrieve top-k most semantically relevant past session transcripts
        for a customer within a brand.

        Returns list of transcript strings (most relevant first).
        """
        col = _get_collection(brand_id)
        try:
            query_customer_id = customer_id or "anonymous"
            results = col.query(
                query_texts=[query],
                n_results=min(k, 10),
                where={"customer_id": query_customer_id},
                include=["documents", "metadatas", "distances"],
            )
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            # Filter by expiry and distance threshold
            now = datetime.now(timezone.utc)
            valid_docs = []
            for doc, meta, dist in zip(docs, metas, distances):
                if dist > 0.7:  # cosine distance > 0.7 → not relevant
                    continue
                expire_at_str = meta.get("expire_at", "")
                if expire_at_str:
                    try:
                        expire_at = datetime.fromisoformat(expire_at_str)
                        if now > expire_at:
                            continue
                    except ValueError:
                        pass
                valid_docs.append(doc)

            log.debug(
                "mempalace_recalled",
                customer_id=customer_id,
                brand_id=brand_id,
                returned=len(valid_docs),
            )
            return valid_docs
        except Exception as exc:
            log.error("mempalace_recall_failed", error=str(exc), customer_id=customer_id)
            return []

    def forget_expired(self, brand_id: str) -> int:
        """Delete all sessions past their 30-day retention window. Returns count deleted."""
        col = _get_collection(brand_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            # ChromaDB where filter with $lt operator
            results = col.get(
                where={"expire_at": {"$lt": now_iso}},
                include=["metadatas"],
            )
            ids = results.get("ids", [])
            if ids:
                col.delete(ids=ids)
                log.info("mempalace_expired_purged", brand_id=brand_id, count=len(ids))
            return len(ids)
        except Exception as exc:
            log.error("mempalace_purge_failed", error=str(exc), brand_id=brand_id)
            return 0

    def get_session_count(self, brand_id: str, customer_id: str | None) -> int:
        """Return number of stored sessions for a customer."""
        col = _get_collection(brand_id)
        if not customer_id:
            return 0
        try:
            results = col.get(
                where={"customer_id": customer_id},
                include=[],
            )
            return len(results.get("ids", []))
        except Exception:
            return 0


# Module-level singleton
mempalace = MemPalaceClient()
