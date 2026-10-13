"""Add playbook AB testing columns to csat_mitigations.

Revision ID: 009
Revises: 008
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("csat_mitigations", sa.Column("variant_group", sa.String(10), server_default="A", nullable=False))
    op.add_column("csat_mitigations", sa.Column("recovery_converted", sa.Boolean, server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("csat_mitigations", "recovery_converted")
    op.drop_column("csat_mitigations", "variant_group")
