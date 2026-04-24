-- ═══════════════════════════════════════════════════════════════════════════════
-- Docker bootstrap only — managed by Alembic after initial migration.
-- Alembic tracks its own version in the `alembic_version` table.
-- This file runs on first container start; subsequent starts skip existing tables.
-- After init.sql, docker-compose runs `alembic upgrade head` which sees
-- alembic_version=001 and exits cleanly (no-op).
-- ═══════════════════════════════════════════════════════════════════════════════

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for full-text search

-- ─── Knowledge chunk embeddings (vector fallback) ──────────────────────
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id    VARCHAR(128) NOT NULL UNIQUE,
    brand_id    VARCHAR(64) NOT NULL,
    source_url  TEXT,
    content     TEXT NOT NULL,
    chunk_type  VARCHAR(32),   -- faq, policy, resolution_step, product
    embedding   vector,  -- text-embedding-3-small dimensions
    confidence  FLOAT,         -- LLM extraction confidence (0.0-1.0)
    metadata    JSONB DEFAULT '{}',
    crawled_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX ON knowledge_chunks (brand_id);
CREATE INDEX ON knowledge_chunks USING GIN (metadata);

-- ─── Support session log ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS support_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id        VARCHAR(64) NOT NULL,
    customer_id     VARCHAR(128),
    channel         VARCHAR(16) NOT NULL,  -- chat, email, voice, whatsapp
    intent          VARCHAR(64),
    outcome         VARCHAR(32),           -- resolved, escalated, abandoned
    resolution_id   VARCHAR(128),
    turn_count      INT DEFAULT 0,
    satisfaction    FLOAT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX ON support_sessions (brand_id, intent);
CREATE INDEX ON support_sessions (customer_id);
CREATE INDEX ON support_sessions (outcome);

-- ─── Unknown intent label queue ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS intent_label_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id        VARCHAR(64) NOT NULL,
    raw_message     TEXT NOT NULL,
    suggested_label VARCHAR(64),
    reviewed        BOOLEAN DEFAULT FALSE,
    approved_label  VARCHAR(64),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Resolution review queue ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS resolution_review_queue (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id            VARCHAR(64) NOT NULL,
    issue_type          VARCHAR(64),
    resolution_tried    VARCHAR(128),
    failure_reason      TEXT,
    conversation        JSONB,
    reviewed            BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Crawl source registry ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crawl_sources (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id        VARCHAR(64) NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    source_type     VARCHAR(32),  -- faq, policy, help, product
    last_crawled_at TIMESTAMPTZ,
    last_hash       VARCHAR(64),  -- content hash for change detection
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON crawl_sources (brand_id);

-- ═══════════════════════════════════════════════════════════════════════
-- Brand API Registry — dynamic LLM tools per brand
-- ═══════════════════════════════════════════════════════════════════════

-- Per-brand API configuration
CREATE TABLE IF NOT EXISTS brand_api_config (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id        VARCHAR(64) NOT NULL UNIQUE,
    base_url        TEXT NOT NULL,
    description     TEXT,
    default_headers JSONB DEFAULT '{}',
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Individual endpoints registered for a brand
CREATE TABLE IF NOT EXISTS registered_api_endpoints (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_api_config_id UUID NOT NULL REFERENCES brand_api_config(id) ON DELETE CASCADE,
    name                VARCHAR(128) NOT NULL,
    description         TEXT,
    method              VARCHAR(8) NOT NULL,           -- GET, POST, PUT, DELETE
    path                TEXT NOT NULL,
    query_params        JSONB DEFAULT '[]',            -- [{name, type, required, description}]
    body_schema         JSONB,                          -- JSON Schema for body
    response_schema     JSONB,                          -- parsed response shape
    rate_limit_per_min  INT,
    requires_auth       BOOLEAN DEFAULT FALSE,
    tags                TEXT[] DEFAULT '{}',           -- e.g. orders, returns, customer
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(brand_api_config_id, name)
);

-- Per-brand encrypted credentials
CREATE TABLE IF NOT EXISTS brand_api_credentials (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_api_config_id UUID NOT NULL REFERENCES brand_api_config(id) ON DELETE CASCADE,
    key_type            VARCHAR(32) NOT NULL,  -- api_key, bearer_token, basic_auth, oauth2
    key_name            VARCHAR(64) NOT NULL,  -- human label (e.g. "Primary API Key")
    encrypted_value     BYTEA NOT NULL,         -- Fernet-encrypted secret
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    is_active           BOOLEAN DEFAULT TRUE,
    UNIQUE(brand_api_config_id, key_type)
);

CREATE INDEX ON brand_api_config (brand_id);
CREATE INDEX ON registered_api_endpoints (brand_api_config_id, is_active);
CREATE INDEX ON brand_api_credentials (brand_api_config_id);

-- ═══════════════════════════════════════════════════════════════════════════════
-- Alembic version stamp — must match alembic/versions/001_initial_schema.py.
-- This lets `alembic upgrade head` (run in docker-compose command) detect
-- that the DB is already at the correct revision and exit cleanly (no-op).
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
INSERT INTO alembic_version (version_num) VALUES ('001')
    ON CONFLICT DO NOTHING;

-- ═══════════════════════════════════════════════════════════════════════════════
-- END BOOTSTRAP — Schema is managed by Alembic after this point.
-- Run `alembic upgrade head` after container start for migration control.
-- ═══════════════════════════════════════════════════════════════════════════════
