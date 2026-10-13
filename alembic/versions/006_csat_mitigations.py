"""Add csat_mitigations table.

Revision ID: 006
Revises: 005
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "csat_mitigations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("support_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.String(255), nullable=True),
        sa.Column("satisfaction", sa.Float, nullable=False),
        sa.Column("suggested_playbook", sa.String(255), nullable=False),
        sa.Column("playbook_details", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(50), default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("csat_mitigations")
