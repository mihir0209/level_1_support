#!/usr/bin/env python3
"""
reset_db.py — Wipe L1 Support AI data stores cleanly.

Stores cleared:
  1. FalkorDB  — l1_support graph   (brand nodes + KB + ontology)
  2. FalkorDB  — l1_user_memory     (Graphiti customer memory)
  3. PostgreSQL — kb_chunks table   (pgvector embeddings)
  4. ChromaDB  — mempalace collections (session transcripts)
  5. Redis     — session keys

Usage:
  # Full reset — everything
  python3 scripts/reset_db.py

  # Brand-specific reset (wherever brand_id filtering is possible)
  python3 scripts/reset_db.py --brand-id coast_uk

  # Preview what would be deleted (no writes)
  python3 scripts/reset_db.py --dry-run
  python3 scripts/reset_db.py --brand-id coast_uk --dry-run

  # Skip confirmation prompt
  python3 scripts/reset_db.py --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog

log = structlog.get_logger("reset_db")


# ─── helpers ──────────────────────────────────────────────────────────────────

def _confirm(message: str) -> bool:
    answer = input(f"\n{message} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _fmt(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


# ─── 1. FalkorDB: l1_support knowledge graph ─────────────────────────────────

def reset_falkordb_knowledge(brand_id: str | None, dry_run: bool) -> None:
    print("\n[FalkorDB] l1_support graph")
    try:
        from falkordb import FalkorDB
        from config import settings
        from core.ontology.schema import GRAPH_NAME

        db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)

        if brand_id is None:
            # Full reset — delete the whole graph
            try:
                result = db.select_graph(GRAPH_NAME).query("MATCH (n) RETURN count(n) AS c")
                total = result.result_set[0][0] if result.result_set else 0
            except Exception:
                total = "?"
            print(f"  Would delete entire graph '{GRAPH_NAME}' (~{total} nodes)")
            if not dry_run:
                try:
                    db.delete_graph(GRAPH_NAME)
                    print(f"  ✓ Graph '{GRAPH_NAME}' deleted")
                except Exception as e:
                    print(f"  ✗ Delete failed (may not exist yet): {e}")
        else:
            # Brand-specific — delete all nodes with matching brand_id
            graph = db.select_graph(GRAPH_NAME)

            # Count what we'd delete
            counts: list[tuple[str, int]] = []
            label_queries = [
                ("brand-owned nodes (brand_id prop)",
                 "MATCH (n) WHERE n.brand_id = $bid RETURN count(n)"),
                ("Brand node",
                 "MATCH (b:Brand {id: $bid}) RETURN count(b)"),
                ("OntologyType",
                 "MATCH (ot:OntologyType {brand_id: $bid}) RETURN count(ot)"),
                ("OntologyProposal",
                 "MATCH (op:OntologyProposal {brand_id: $bid}) RETURN count(op)"),
            ]
            for desc, cypher in label_queries:
                try:
                    r = graph.query(cypher, {"bid": brand_id})
                    n = r.result_set[0][0] if r.result_set else 0
                    counts.append((desc, n))
                except Exception as e:
                    counts.append((desc, f"err:{e}"))

            for desc, n in counts:
                print(f"  Would delete {n} {desc}")

            if not dry_run:
                deletions = [
                    ("brand_id-property nodes",
                     "MATCH (n) WHERE n.brand_id = $bid DETACH DELETE n"),
                    ("Brand node",
                     "MATCH (b:Brand {id: $bid}) DETACH DELETE b"),
                ]
                for desc, cypher in deletions:
                    try:
                        graph.query(cypher, {"bid": brand_id})
                        print(f"  ✓ Deleted {desc}")
                    except Exception as e:
                        print(f"  ✗ Failed ({desc}): {e}")

    except ImportError as e:
        print(f"  ⚠ Skipped (falkordb not available): {e}")
    except Exception as e:
        print(f"  ✗ Error: {e}")


# ─── 2. FalkorDB: l1_user_memory (Graphiti customer memory) ──────────────────

def reset_falkordb_graphiti(brand_id: str | None, dry_run: bool) -> None:
    print("\n[FalkorDB] l1_user_memory graph (Graphiti customer memory)")
    try:
        from falkordb import FalkorDB
        from config import settings

        graphiti_graph = settings.graphiti_graph_name
        db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)

        if brand_id is None:
            try:
                result = db.select_graph(graphiti_graph).query("MATCH (n) RETURN count(n) AS c")
                total = result.result_set[0][0] if result.result_set else 0
            except Exception:
                total = "?"
            print(f"  Would delete entire graph '{graphiti_graph}' (~{total} nodes)")
            if not dry_run:
                try:
                    db.delete_graph(graphiti_graph)
                    print(f"  ✓ Graph '{graphiti_graph}' deleted")
                except Exception as e:
                    print(f"  ✗ Delete failed (may not exist yet): {e}")
        else:
            # Graphiti uses group_id = "{brand_id}:{customer_id}"
            prefix = f"{brand_id}:"
            graph = db.select_graph(graphiti_graph)
            try:
                r = graph.query(
                    "MATCH (n) WHERE n.group_id STARTS WITH $prefix RETURN count(n)",
                    {"prefix": prefix},
                )
                n = r.result_set[0][0] if r.result_set else 0
                print(f"  Would delete {n} nodes with group_id starting with '{prefix}'")
                if not dry_run:
                    graph.query(
                        "MATCH (n) WHERE n.group_id STARTS WITH $prefix DETACH DELETE n",
                        {"prefix": prefix},
                    )
                    print(f"  ✓ Deleted Graphiti nodes for brand '{brand_id}'")
            except Exception as e:
                print(f"  ✗ Error: {e}")

    except ImportError as e:
        print(f"  ⚠ Skipped (falkordb not available): {e}")
    except Exception as e:
        print(f"  ✗ Error: {e}")


# ─── 3. PostgreSQL: kb_chunks ─────────────────────────────────────────────────

def reset_postgres(brand_id: str | None, dry_run: bool) -> None:
    print("\n[PostgreSQL] kb_chunks table")
    try:
        import psycopg
        from config import settings

        dsn = (
            f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
        )

        with psycopg.connect(dsn) as conn:
            if brand_id is None:
                row = conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()
                total = row[0] if row else 0
                print(f"  Would delete {_fmt(total, 'row')} from kb_chunks")
                if not dry_run:
                    conn.execute("TRUNCATE TABLE kb_chunks")
                    conn.commit()
                    print("  ✓ kb_chunks truncated")
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM kb_chunks WHERE brand_id = %s", (brand_id,)
                ).fetchone()
                total = row[0] if row else 0
                print(f"  Would delete {_fmt(total, 'row')} for brand '{brand_id}'")
                if not dry_run:
                    conn.execute("DELETE FROM kb_chunks WHERE brand_id = %s", (brand_id,))
                    conn.commit()
                    print(f"  ✓ Deleted {total} rows for brand '{brand_id}'")

    except Exception as e:
        print(f"  ✗ Error (table may not exist yet): {e}")


# ─── 4. ChromaDB: MemPalace collections ──────────────────────────────────────

def reset_chromadb(brand_id: str | None, dry_run: bool) -> None:
    print("\n[ChromaDB] MemPalace session storage")
    try:
        import chromadb
        from config import settings

        data_dir = os.path.abspath(settings.mempalace_data_dir)
        if not os.path.exists(data_dir):
            print(f"  ✓ Nothing to do (data dir {data_dir} doesn't exist)")
            return

        client = chromadb.PersistentClient(path=data_dir)
        all_collections = client.list_collections()

        if brand_id is None:
            mp_collections = [c for c in all_collections if c.name.startswith("mp_")]
            print(f"  Would delete {len(mp_collections)} MemPalace collection(s): "
                  f"{[c.name for c in mp_collections]}")
            if not dry_run:
                for col in mp_collections:
                    client.delete_collection(col.name)
                print(f"  ✓ Deleted {len(mp_collections)} collection(s)")
        else:
            safe = brand_id.replace(".", "_").replace("-", "_")[:50]
            col_name = f"mp_{safe}"
            match = next((c for c in all_collections if c.name == col_name), None)
            if match:
                count = match.count()
                print(f"  Would delete collection '{col_name}' ({count} documents)")
                if not dry_run:
                    client.delete_collection(col_name)
                    print(f"  ✓ Deleted collection '{col_name}'")
            else:
                print(f"  ✓ Nothing to do (collection '{col_name}' not found)")

    except Exception as e:
        print(f"  ✗ Error: {e}")


# ─── 5. Redis: session keys ───────────────────────────────────────────────────

def reset_redis(brand_id: str | None, dry_run: bool) -> None:
    print("\n[Redis] session cache")
    try:
        import redis
        from config import settings

        # Parse redis URL for sync client
        r = redis.from_url(settings.redis_url, decode_responses=True)

        if brand_id is None:
            # Count session:* keys
            keys = list(r.scan_iter("session:*"))
            print(f"  Would delete {_fmt(len(keys), 'session key')}")
            if not dry_run:
                if keys:
                    r.delete(*keys)
                print(f"  ✓ Deleted {len(keys)} session keys")
        else:
            # Scan and filter by brand_id in JSON payload
            matching: list[str] = []
            for key in r.scan_iter("session:*"):
                try:
                    val = r.get(key)
                    if val:
                        data = json.loads(val)
                        if data.get("brand_id") == brand_id:
                            matching.append(key)
                except Exception:
                    pass
            print(f"  Would delete {_fmt(len(matching), 'session key')} for brand '{brand_id}'")
            if not dry_run and matching:
                r.delete(*matching)
                print(f"  ✓ Deleted {len(matching)} session keys")

    except Exception as e:
        print(f"  ✗ Error (Redis may not be running): {e}")


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset L1 Support AI data stores.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--brand-id",
        metavar="BRAND_ID",
        default=None,
        help="Clear only this brand's data (e.g. coast_uk). Omit for full reset.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without making any changes.",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    parser.add_argument(
        "--skip-graphiti",
        action="store_true",
        help="Skip the Graphiti (l1_user_memory) graph.",
    )
    args = parser.parse_args()

    scope = f"brand '{args.brand_id}'" if args.brand_id else "ALL BRANDS (full reset)"
    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}L1 Support DB Reset — scope: {scope}")
    print("─" * 60)

    if not args.dry_run and not args.yes:
        if not _confirm(f"This will permanently delete data for {scope}. Continue?"):
            print("Aborted.")
            sys.exit(0)

    reset_falkordb_knowledge(args.brand_id, args.dry_run)
    if not args.skip_graphiti:
        reset_falkordb_graphiti(args.brand_id, args.dry_run)
    reset_postgres(args.brand_id, args.dry_run)
    reset_chromadb(args.brand_id, args.dry_run)
    reset_redis(args.brand_id, args.dry_run)

    print("\n" + "─" * 60)
    if args.dry_run:
        print("Dry run complete — no changes made. Re-run without --dry-run to apply.")
    else:
        print("Reset complete.")
        if not args.brand_id:
            print("Run seed scripts to restore baseline data:")
            print("  python3 -m data.boohoo_kb.seed_boohoo")


if __name__ == "__main__":
    main()
