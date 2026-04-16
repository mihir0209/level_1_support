"""
FalkorDB graph schema management.
Creates indexes and constraints. Idempotent — safe to run on every startup.
"""

from __future__ import annotations

import structlog
from falkordb import FalkorDB

from config import settings

log = structlog.get_logger(__name__)

# Graph name — all brand data lives in one graph, isolated by brand_id property
GRAPH_NAME = "l1_support"


def get_graph_client() -> tuple[FalkorDB, object]:
    """Return (db_client, graph) — graph is the query interface."""
    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    graph = db.select_graph(GRAPH_NAME)
    return db, graph


def create_schema(graph) -> None:
    """
    Create indexes and constraints on the FalkorDB graph.
    FalkorDB uses Cypher; indexes speed up brand_id-filtered lookups.
    """
    indexes = [
        # Brand indexes
        "CREATE INDEX FOR (b:Brand) ON (b.id)",
        "CREATE INDEX FOR (b:Brand) ON (b.group_id)",

        # IssueType indexes
        "CREATE INDEX FOR (it:IssueType) ON (it.id)",
        "CREATE INDEX FOR (it:IssueType) ON (it.category)",
        "CREATE INDEX FOR (it:IssueType) ON (it.brand_id)",

        # Resolution indexes
        "CREATE INDEX FOR (r:Resolution) ON (r.id)",

        # Policy indexes
        "CREATE INDEX FOR (p:ReturnPolicy) ON (p.brand_id)",
        "CREATE INDEX FOR (p:ShippingPolicy) ON (p.brand_id)",
        "CREATE INDEX FOR (p:ShippingPolicy) ON (p.region)",

        # Customer / Order indexes
        "CREATE INDEX FOR (c:Customer) ON (c.id)",
        "CREATE INDEX FOR (c:Customer) ON (c.brand_id)",
        "CREATE INDEX FOR (o:Order) ON (o.id)",
        "CREATE INDEX FOR (o:Order) ON (o.customer_id)",
        "CREATE INDEX FOR (o:Order) ON (o.brand_id)",
    ]

    for cypher in indexes:
        try:
            graph.query(cypher)
        except Exception as e:
            # Index may already exist — not an error
            if "already indexed" not in str(e).lower():
                log.warning("index_creation_warning", cypher=cypher, error=str(e))

    log.info("schema_created", graph=GRAPH_NAME)


def verify_connectivity() -> bool:
    """Health check — returns True if FalkorDB is reachable."""
    try:
        _, graph = get_graph_client()
        graph.query("RETURN 1")
        return True
    except Exception as e:
        log.error("falkordb_connection_failed", error=str(e))
        return False
