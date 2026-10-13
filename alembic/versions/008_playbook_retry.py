"""Add playbook execution retry columns to csat_mitigations.

Revision ID: 008
Revises: 007
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("csat_mitigations", sa.Column("retry_count", sa.Integer, server_default="0", default=0))
    op.add_column("csat_mitigations", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("csat_mitigations", "next_retry_at")
    op.drop_column("csat_mitigations", "retry_count")
