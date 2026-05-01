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


def ensure_graphiti_graph() -> bool:
    """
    Ensure the Graphiti graph exists in FalkorDB.
    Graphiti uses a separate graph name (l1_user_memory) from the main KG (l1_support).
    If it doesn't exist, create it. Idempotent — safe to call on every startup.
    """
    try:
        from config import settings
        db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
        graph_name = settings.graphiti_graph_name

        # Check if graph exists by trying to select it
        try:
            existing = db.select_graph(graph_name)
            existing.query("RETURN 1")
            log.info("graphiti_graph_exists", graph=graph_name)
            return True
        except Exception:
            # Graph doesn't exist — create it
            pass

        db.select_graph(graph_name)  # creates implicitly
        log.info("graphiti_graph_created", graph=graph_name)
        return True
    except Exception as e:
        log.error("graphiti_graph_init_failed", error=str(e))
        return False
