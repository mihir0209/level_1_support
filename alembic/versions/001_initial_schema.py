"""Initial schema — all Postgres tables managed by Alembic.

Revision ID: 001
Revises:
Create Date: 2026-04-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Extensions ──────────────────────────────────────────────────────────────
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    # ── knowledge_chunks ─────────────────────────────────────────────────────────
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("chunk_id", sa.String(128), nullable=False, unique=True),
        sa.Column("brand_id", sa.String(64), nullable=False, index=True),
        sa.Column("source_url", sa.Text, nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("chunk_type", sa.String(32), nullable=True),
        sa.Column("embedding", sa.Text, nullable=True),  # pgvector stored as text
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("metadata", postgresql.JSONB, default={}),
        sa.Column("crawled_at", sa.DateTime(timezone=True), default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_knowledge_chunks_brand_id", "knowledge_chunks", ["brand_id"])
    op.create_index(
        "ix_knowledge_chunks_embedding", "knowledge_chunks", ["embedding"],
        postgresql_using="ivfflat", postgresql_with={"lists": 100}
    )
    op.create_index(
        "ix_knowledge_chunks_meta", "knowledge_chunks", ["metadata"],
        postgresql_using="gin"
    )

    # ── support_sessions ───────────────────────────────────────────────────────
    op.create_table(
        "support_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("brand_id", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(128), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("intent", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("resolution_id", sa.String(128), nullable=True),
        sa.Column("turn_count", sa.Integer, default=0),
        sa.Column("satisfaction", sa.Float, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB, default={}),
    )
    op.create_index("ix_support_sessions_brand_intent", "support_sessions", ["brand_id", "intent"])
    op.create_index("ix_support_sessions_customer", "support_sessions", ["customer_id"])
    op.create_index("ix_support_sessions_outcome", "support_sessions", ["outcome"])

    # ── intent_label_queue ──────────────────────────────────────────────────────
    op.create_table(
        "intent_label_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("brand_id", sa.String(64), nullable=False),
        sa.Column("raw_message", sa.Text, nullable=False),
        sa.Column("suggested_label", sa.String(64), nullable=True),
        sa.Column("reviewed", sa.Boolean, default=False),
        sa.Column("approved_label", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
    )

    # ── resolution_review_queue ──────────────────────────────────────────────────
    op.create_table(
        "resolution_review_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("brand_id", sa.String(64), nullable=False),
        sa.Column("issue_type", sa.String(64), nullable=True),
        sa.Column("resolution_tried", sa.String(128), nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("conversation", postgresql.JSONB, nullable=True),
        sa.Column("reviewed", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
    )

    # ── crawl_sources ───────────────────────────────────────────────────────────
    op.create_table(
        "crawl_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("brand_id", sa.String(64), nullable=False),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("source_type", sa.String(32), nullable=True),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_hash", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
    )
    op.create_index("ix_crawl_sources_brand", "crawl_sources", ["brand_id"])

    # ── brand_api_config ────────────────────────────────────────────────────────
    op.create_table(
        "brand_api_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("brand_id", sa.String(64), nullable=False, unique=True),
        sa.Column("base_url", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("default_headers", postgresql.JSONB, default={}),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), default=sa.func.now(), onupdate=sa.func.now()),
    )

    # ── registered_api_endpoints ────────────────────────────────────────────────
    op.create_table(
        "registered_api_endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("brand_api_config_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("brand_api_config.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("query_params", postgresql.JSONB, default=[]),
        sa.Column("body_schema", postgresql.JSONB, nullable=True),
        sa.Column("response_schema", postgresql.JSONB, nullable=True),
        sa.Column("rate_limit_per_min", sa.Integer, nullable=True),
        sa.Column("requires_auth", sa.Boolean, default=False),
        sa.Column("tags", postgresql.ARRAY(sa.String), default=[]),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), default=sa.func.now(), onupdate=sa.func.now()),
        sa.UniqueConstraint("brand_api_config_id", "name", name="uq_endpoint_config_name"),
    )
    op.create_index("ix_endpoints_config_active", "registered_api_endpoints", ["brand_api_config_id", "is_active"])

    # ── brand_api_credentials ───────────────────────────────────────────────────
    op.create_table(
        "brand_api_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("brand_api_config_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("brand_api_config.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_type", sa.String(32), nullable=False),
        sa.Column("key_name", sa.String(64), nullable=False),
        sa.Column("encrypted_value", sa.LargeBinary, nullable=False),
        sa.Column("key_version", sa.Integer, default=1),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.UniqueConstraint("brand_api_config_id", "key_type", name="uq_cred_config_type"),
    )
    op.create_index("ix_credentials_config", "brand_api_credentials", ["brand_api_config_id"])


def downgrade() -> None:
    op.drop_table("brand_api_credentials")
    op.drop_table("registered_api_endpoints")
    op.drop_table("brand_api_config")
    op.drop_table("crawl_sources")
    op.drop_table("resolution_review_queue")
    op.drop_table("intent_label_queue")
    op.drop_table("support_sessions")
    op.drop_table("knowledge_chunks")