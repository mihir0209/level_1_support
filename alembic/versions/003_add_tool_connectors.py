"""Add connector fields to registered_api_endpoints.

Revision ID: 003
Revises: 002
Create Date: 2026-06-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("registered_api_endpoints", sa.Column("connector_type", sa.String(32), server_default="http", nullable=False))
    op.add_column("registered_api_endpoints", sa.Column("connector_config", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("registered_api_endpoints", "connector_config")
    op.drop_column("registered_api_endpoints", "connector_type")
