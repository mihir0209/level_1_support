"""Add session_audit_logs table.

Revision ID: 005
Revises: 004
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(100), index=True, nullable=False),
        sa.Column("brand_id", sa.String(50), nullable=False),
        sa.Column("customer_id", sa.String(50), nullable=True),
        sa.Column("intent_trajectory", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("tools_executed", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("policies_matched", postgresql.JSONB, server_default="[]", nullable=False),
        sa.Column("compliance_score", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("compliance_violations", sa.Text(), nullable=True),
        sa.Column("audit_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("session_audit_logs")
