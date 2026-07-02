"""
SQLAlchemy ORM models for all Postgres tables.
Used by Alembic migrations and application code alike.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, Uuid


class Base(DeclarativeBase):
    pass


# ── Custom types ───────────────────────────────────────────────────────────────

class Vector(TypeDecorator):
    """pgvector column type for embedding storage."""
    impl = Text
    cache_ok = True

    def __init__(self, dim: int = 1536):
        super().__init__()
        self.dim = dim

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, list):
            return "[" + ",".join(str(v) for v in value) + "]"
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("["):
            return [float(x) for x in value.strip("[]").split(",")]
        return value


# ── Models ───────────────────────────────────────────────────────────────────

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    brand_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding: Mapped[str | None] = mapped_column(Vector, nullable=True)  # pgvector as text
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_knowledge_chunks_embedding", "embedding", postgresql_using="ivfflat", postgresql_with={"lists": 100}),
        Index("ix_knowledge_chunks_meta", "metadata", postgresql_using="gin"),
    )


class SupportSession(Base):
    __tablename__ = "support_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    satisfaction: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    __table_args__ = (
        Index("ix_support_sessions_brand_intent", "brand_id", "intent"),
        Index("ix_support_sessions_customer", "customer_id"),
        Index("ix_support_sessions_outcome", "outcome"),
    )


class IntentLabelQueue(Base):
    __tablename__ = "intent_label_queue"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_message: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ResolutionReviewQueue(Base):
    __tablename__ = "resolution_review_queue"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_tried: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class CrawlSource(Base):
    __tablename__ = "crawl_sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_crawl_sources_brand", "brand_id"),
    )


class BrandApiConfig(Base):
    __tablename__ = "brand_api_config"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    endpoints: Mapped[list["RegisteredApiEndpoint"]] = relationship(
        "RegisteredApiEndpoint", back_populates="config", cascade="all, delete-orphan"
    )
    credentials: Mapped[list["BrandApiCredential"]] = relationship(
        "BrandApiCredential", back_populates="config", cascade="all, delete-orphan"
    )


class RegisteredApiEndpoint(Base):
    __tablename__ = "registered_api_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_api_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_api_config.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    query_params: Mapped[list] = mapped_column(JSONB, default=list)
    body_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requires_auth: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    connector_type: Mapped[str] = mapped_column(String(32), default="http", nullable=False)
    connector_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("brand_api_config_id", "name", name="uq_endpoint_config_name"),
        Index("ix_endpoints_config_active", "brand_api_config_id", "is_active"),
    )

    config: Mapped["BrandApiConfig"] = relationship("BrandApiConfig", back_populates="endpoints")


class BrandApiCredential(Base):
    __tablename__ = "brand_api_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    brand_api_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_api_config.id", ondelete="CASCADE"), nullable=False
    )
    key_type: Mapped[str] = mapped_column(String(32), nullable=False)  # api_key | bearer_token | basic_auth | oauth2
    key_name: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("brand_api_config_id", "key_type", name="uq_cred_config_type"),
        Index("ix_credentials_config", "brand_api_config_id"),
    )

    config: Mapped["BrandApiConfig"] = relationship("BrandApiConfig", back_populates="credentials")


class RegisteredBrand(Base):
    __tablename__ = "registered_brands"

    brand_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    domain: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), default="UTC", server_default="UTC")
    locale: Mapped[str] = mapped_column(String(10), default="en_US", server_default="en_US")
    brand_voice: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SessionAuditLog(Base):
    __tablename__ = "session_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    brand_id: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    intent_trajectory: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    tools_executed: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    policies_matched: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    compliance_score: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    compliance_violations: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
