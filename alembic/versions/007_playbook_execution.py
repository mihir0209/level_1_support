"""Add playbook execution audit columns to csat_mitigations.

Revision ID: 007
Revises: 006
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("csat_mitigations", sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("csat_mitigations", sa.Column("execution_error", sa.Text, nullable=True))
    op.add_column("csat_mitigations", sa.Column("execution_response", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("csat_mitigations", "execution_response")
    op.drop_column("csat_mitigations", "execution_error")
    op.drop_column("csat_mitigations", "executed_at")
