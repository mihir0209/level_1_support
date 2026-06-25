-- ═══════════════════════════════════════════════════════════════════════════════
-- Docker bootstrap only — creates extensions.
-- Tables are managed by Alembic (alembic/versions/).
-- Alembic will insert its version stamp into alembic_version table.
-- ═══════════════════════════════════════════════════════════════════════════════

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for full-text search

-- ═══════════════════════════════════════════════════════════════════════════════
-- END BOOTSTRAP — Schema is managed by Alembic after this point.
-- ═══════════════════════════════════════════════════════════════════════════════