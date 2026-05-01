"""Add read-only Postgres role for Text2Cypher queries.

Revision ID: 002
Revises: 001
Create Date: 2026-05-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create a read-only role for Text2Cypher queries.
    # This role can ONLY SELECT — it literally cannot write via SQL.
    # The keyword-block approach in retriever.py remains as a second-layer
    # defense, but Postgres enforcement is the primary safety measure.
    op.execute(sa.text("""
        DO $$
        BEGIN
            -- Create the role if it doesn't exist
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'l1_readonly') THEN
                CREATE ROLE l1_readonly WITH LOGIN PASSWORD 'l1_readonly_secret';
            END IF;

            -- Grant CONNECT on the app database
            GRANT CONNECT ON DATABASE l1_support TO l1_readonly;

            -- Grant USAGE on the public schema (for function access)
            GRANT USAGE ON SCHEMA public TO l1_readonly;

            -- Grant SELECT on all existing tables in public schema
            GRANT SELECT ON ALL TABLES IN SCHEMA public TO l1_readonly;

            -- Ensure future tables in public schema are also readable
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO l1_readonly;

            -- Revoke all write privileges explicitly (belt-and-suspenders)
            REVOKE INSERT ON ALL TABLES IN SCHEMA public FROM l1_readonly;
            REVOKE UPDATE ON ALL TABLES IN SCHEMA public FROM l1_readonly;
            REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM l1_readonly;
            REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM l1_readonly;
            REVOKE CREATE ON SCHEMA public FROM l1_readonly;

            -- Disallow schema modifications
            REVOKE USAGE ON SCHEMA pg_catalog FROM l1_readonly;
            REVOKE SELECT ON pg_proc FROM l1_readonly;
        END
        $$;
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP ROLE IF EXISTS l1_readonly"))