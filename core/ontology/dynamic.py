"""
Dynamic ontology manager — agents can propose and apply new entity types.

Domain-specific only: entity types describe brand knowledge (products, policies,
services) NOT customer data. Customer nodes use the fixed Customer/Order schema.

Entity types are stored as OntologyType nodes in FalkorDB so the schema lives
alongside the data and survives restarts.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Built-in domain entity types — always present, cannot be deleted
_BUILTIN_TYPES = {
    "FAQ": {
        "label": "FAQ",
        "description": "Frequently asked questions with answers",
        "properties": ["question", "answer", "category", "keywords", "brand_id"],
        "relationships": [("Brand", "HAS_FAQ", "incoming")],
        "builtin": True,
    },
    "Policy": {
        "label": "Policy",
        "description": "Brand policy document (return, shipping, privacy, etc.)",
        "properties": ["policy_type", "title", "content", "brand_id", "effective_date"],
        "relationships": [("Brand", "HAS_POLICY", "incoming")],
        "builtin": True,
    },
    "KnowledgeItem": {
        "label": "KnowledgeItem",
        "description": "General structured knowledge extracted from brand pages",
        "properties": ["title", "content", "chunk_type", "url", "brand_id", "extracted_at"],
        "relationships": [("Brand", "HAS_KNOWLEDGE", "incoming")],
        "builtin": True,
    },
    "Review": {
        "label": "Review",
        "description": "Customer review or external mention of the brand",
        "properties": ["content", "sentiment", "source_url", "brand_id", "published_at"],
        "relationships": [("Brand", "HAS_REVIEW", "incoming")],
        "builtin": True,
    },
    "ProductCategory": {
        "label": "ProductCategory",
        "description": "A category of products offered by the brand",
        "properties": ["name", "slug", "description", "brand_id"],
        "relationships": [("Brand", "SELLS", "incoming")],
        "builtin": True,
    },
}


class OntologyManager:
    """
    Reads/writes the dynamic ontology schema in FalkorDB.
    Agents call propose_entity_type() to extend the schema;
    the synthesiser calls apply_proposal() or auto_apply_proposals().
    """

    def __init__(self, graph=None):
        self._graph = graph

    def _get_graph(self):
        if self._graph:
            return self._graph
        from core.ontology.schema import get_graph_client
        _, g = get_graph_client()
        return g

    # ── Read ──────────────────────────────────────────────────────────────

    def get_entity_types(self, include_builtin: bool = True) -> list[dict[str, Any]]:
        """All active entity types (builtin + dynamically added)."""
        types: list[dict[str, Any]] = []

        if include_builtin:
            types.extend(_BUILTIN_TYPES.values())

        try:
            graph = self._get_graph()
            result = graph.query(
                "MATCH (ot:OntologyType {status: 'active'}) "
                "RETURN ot.label, ot.description, ot.properties, ot.relationships, ot.brand_id"
            )
            for row in result.result_set:
                label, desc, props_raw, rels_raw, brand_id = row
                if label in _BUILTIN_TYPES:
                    continue  # already included above
                types.append({
                    "label": label,
                    "description": desc or "",
                    "properties": json.loads(props_raw) if props_raw else [],
                    "relationships": json.loads(rels_raw) if rels_raw else [],
                    "brand_id": brand_id,
                    "builtin": False,
                })
        except Exception as e:
            log.warning("ontology_read_failed", error=str(e))

        return types

    def get_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        """
        Return ontology proposals filtered by status.
        status=None returns all (pending + active).
        status='pending' returns only unapproved proposals.
        status='active' returns auto-applied or manually approved proposals.
        """
        try:
            graph = self._get_graph()
            where = f"WHERE op.status = '{status}'" if status else ""
            result = graph.query(
                f"MATCH (op:OntologyProposal) {where} "
                "RETURN op.proposal_id, op.label, op.description, op.properties, "
                "       op.relationships, op.brand_id, op.proposed_by, op.proposed_at, "
                "       op.evidence, op.status"
            )
            proposals = []
            for row in result.result_set:
                pid, label, desc, props_raw, rels_raw, brand_id, proposed_by, proposed_at, evidence, prop_status = row
                proposals.append({
                    "proposal_id": pid,
                    "label": label,
                    "description": desc,
                    "properties": json.loads(props_raw) if props_raw else [],
                    "relationships": json.loads(rels_raw) if rels_raw else [],
                    "brand_id": brand_id,
                    "proposed_by": proposed_by,
                    "proposed_at": proposed_at,
                    "evidence": evidence,
                    "status": prop_status,
                })
            return proposals
        except Exception as e:
            log.warning("proposals_read_failed", error=str(e))
            return []

    def get_pending_proposals(self) -> list[dict[str, Any]]:
        """Backward-compat alias — returns only pending proposals."""
        return self.get_proposals(status="pending")

    # ── Write ─────────────────────────────────────────────────────────────

    def propose_entity_type(
        self,
        label: str,
        description: str,
        properties: list[str],
        relationships: list[tuple[str, str, str]],
        brand_id: str,
        proposed_by: str = "agent",
        evidence: str = "",
        auto_apply: bool = True,
    ) -> str:
        """
        Propose a new entity type for the domain ontology.

        If label already exists (builtin or active), returns 'exists'.
        If auto_apply=True, immediately activates the proposal.
        Returns proposal_id or 'exists'.
        """
        existing = {t["label"] for t in self.get_entity_types()}
        if label in existing:
            log.info("ontology_proposal_skipped", label=label, reason="already_exists")
            return "exists"

        proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()

        try:
            graph = self._get_graph()
            graph.query(
                "MERGE (op:OntologyProposal {proposal_id: $pid}) "
                "SET op.label = $label, op.description = $desc, "
                "    op.properties = $props, op.relationships = $rels, "
                "    op.brand_id = $brand_id, op.proposed_by = $proposed_by, "
                "    op.proposed_at = $now, op.evidence = $evidence, "
                "    op.status = $status",
                {
                    "pid": proposal_id,
                    "label": label,
                    "desc": description,
                    "props": json.dumps(properties),
                    "rels": json.dumps(relationships),
                    "brand_id": brand_id,
                    "proposed_by": proposed_by,
                    "now": now,
                    "evidence": evidence[:500] if evidence else "",
                    "status": "active" if auto_apply else "pending",
                },
            )
            log.info("ontology_proposed", proposal_id=proposal_id, label=label, auto_apply=auto_apply)

            if auto_apply:
                self._activate_proposal(proposal_id, label, description, properties, relationships, brand_id)
        except Exception as e:
            log.error("ontology_proposal_failed", label=label, error=str(e))

        return proposal_id

    def apply_proposal(self, proposal_id: str) -> bool:
        """Manually approve and activate a pending proposal."""
        try:
            graph = self._get_graph()
            result = graph.query(
                "MATCH (op:OntologyProposal {proposal_id: $pid, status: 'pending'}) "
                "RETURN op.label, op.description, op.properties, op.relationships, op.brand_id",
                {"pid": proposal_id},
            )
            if not result.result_set:
                return False
            label, desc, props_raw, rels_raw, brand_id = result.result_set[0]
            props = json.loads(props_raw) if props_raw else []
            rels = json.loads(rels_raw) if rels_raw else []
            self._activate_proposal(proposal_id, label, desc, props, rels, brand_id)
            return True
        except Exception as e:
            log.error("apply_proposal_failed", proposal_id=proposal_id, error=str(e))
            return False

    def _activate_proposal(
        self,
        proposal_id: str,
        label: str,
        description: str,
        properties: list[str],
        relationships: list,
        brand_id: str,
    ) -> None:
        """Create an OntologyType node and mark the proposal active."""
        try:
            graph = self._get_graph()
            graph.query(
                "MERGE (ot:OntologyType {label: $label}) "
                "SET ot.description = $desc, ot.properties = $props, "
                "    ot.relationships = $rels, ot.brand_id = $brand_id, "
                "    ot.status = 'active', ot.created_at = $now",
                {
                    "label": label,
                    "desc": description,
                    "props": json.dumps(properties),
                    "rels": json.dumps(relationships),
                    "brand_id": brand_id,
                    "now": datetime.now(timezone.utc).isoformat(),
                },
            )
            graph.query(
                "MATCH (op:OntologyProposal {proposal_id: $pid}) SET op.status = 'active'",
                {"pid": proposal_id},
            )
            # Dynamically create indexes for the new node type — brand_id is the
            # most-filtered field; also index by id if present in properties.
            self._create_indexes_for_type(label, properties)
            log.info("ontology_type_activated", label=label, brand_id=brand_id)
        except Exception as e:
            log.error("ontology_activate_failed", label=label, error=str(e))

    def _create_indexes_for_type(self, label: str, properties: list[str]) -> None:
        """
        Create FalkorDB indexes for a dynamically activated entity type.
        Always indexes brand_id (multi-tenant isolation). Also indexes 'id'
        if present in the properties list.
        """
        try:
            graph = self._get_graph()
            indexes = [f"CREATE INDEX FOR (n:{label}) ON (n.brand_id)"]
            if "id" in properties:
                indexes.append(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
            for cypher in indexes:
                try:
                    graph.query(cypher)
                except Exception as e:
                    if "already indexed" not in str(e).lower():
                        log.warning("dynamic_index_create_failed", cypher=cypher, label=label, error=str(e))
        except Exception as e:
            log.warning("index_creation_skipped", label=label, error=str(e))

    def merge_entity_type(
        self,
        label: str,
        new_properties: list[str],
        new_relationships: list,
        enhanced_description: str,
        evidence: str,
        brand_id: str,
    ) -> bool:
        """
        Enrich an existing active OntologyType with new properties / relationships.

        Called when the ontology review agent determines that a proposal whose label
        already exists in the schema still contributes new information.
        Returns True if the existing type was actually enriched.
        """
        try:
            graph = self._get_graph()

            # Load current state
            result = graph.query(
                "MATCH (ot:OntologyType {label: $label, status: 'active'}) "
                "RETURN ot.properties, ot.relationships, ot.description",
                {"label": label},
            )
            if not result.result_set:
                log.warning("merge_type_not_found", label=label)
                return False

            props_raw, rels_raw, current_desc = result.result_set[0]
            existing_props: list[str] = json.loads(props_raw) if props_raw else []
            existing_rels: list = json.loads(rels_raw) if rels_raw else []

            # Merge: union of properties (no duplicates)
            merged_props = list(dict.fromkeys(existing_props + new_properties))
            # Merge relationships — unique by (node, rel_type) tuple
            existing_rel_keys = {(r[0], r[1]) for r in existing_rels if len(r) >= 2}
            for rel in new_relationships:
                if len(rel) >= 2 and (rel[0], rel[1]) not in existing_rel_keys:
                    existing_rels.append(rel)
                    existing_rel_keys.add((rel[0], rel[1]))

            changed = (merged_props != existing_props or existing_rels != json.loads(rels_raw or "[]"))
            new_desc = enhanced_description if (enhanced_description and len(enhanced_description) > len(current_desc or "")) else current_desc

            if not changed and new_desc == current_desc:
                log.info("ontology_merge_no_change", label=label)
                return False

            graph.query(
                "MATCH (ot:OntologyType {label: $label}) "
                "SET ot.properties = $props, ot.relationships = $rels, "
                "    ot.description = $desc, ot.merged_at = $now, "
                "    ot.merge_evidence = $evidence",
                {
                    "label": label,
                    "props": json.dumps(merged_props),
                    "rels": json.dumps(existing_rels),
                    "desc": new_desc or current_desc,
                    "now": datetime.now(timezone.utc).isoformat(),
                    "evidence": evidence[:500] if evidence else "",
                },
            )
            log.info("ontology_type_merged", label=label, brand_id=brand_id,
                     new_props=list(set(merged_props) - set(existing_props)))
            return True

        except Exception as e:
            log.error("ontology_merge_failed", label=label, error=str(e))
            return False

    def get_type_labels_for_prompt(self) -> str:
        """Return a comma-separated string of active entity type labels for LLM prompts."""
        types = self.get_entity_types()
        return ", ".join(t["label"] for t in types)


# Module-level singleton
_manager: OntologyManager | None = None


def get_ontology_manager(graph=None) -> OntologyManager:
    global _manager
    if _manager is None or graph is not None:
        _manager = OntologyManager(graph=graph)
    return _manager
