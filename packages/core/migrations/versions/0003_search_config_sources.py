"""search_configs: add sources[] + target_employers[] for Phase 2 connectors

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "search_configs",
        sa.Column(
            "sources", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")
        ),
    )
    op.add_column(
        "search_configs",
        sa.Column(
            "target_employers",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("search_configs", "target_employers")
    op.drop_column("search_configs", "sources")
