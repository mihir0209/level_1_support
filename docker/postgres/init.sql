-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for full-text search

-- ─── Knowledge chunk embeddings (vector fallback) ──────────────────────
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id    VARCHAR(64) NOT NULL,
    source_url  TEXT,
    content     TEXT NOT NULL,
    chunk_type  VARCHAR(32),   -- faq, policy, resolution_step, product
    embedding   vector(1536),  -- text-embedding-3-small dimensions
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
