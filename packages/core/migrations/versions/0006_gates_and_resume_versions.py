"""Phase 6: human-in-the-loop gate configs + resume versioning.

``gate_configs`` holds per-user toggles for the review gates (after_scout,
after_qualify, after_analyze, before_apply). ``resumes`` gains versioning columns:
``source`` (upload | assembled), ``label`` (human tag), and ``parent_id`` (the resume
an assembled version derived from). The original resume is preserved as its own row.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gate_configs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("gate_key", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_gate_configs_user_id", "gate_configs", ["user_id"])
    op.create_unique_constraint("uq_gate_per_user", "gate_configs", ["user_id", "gate_key"])

    op.add_column(
        "resumes",
        sa.Column("source", sa.String(16), nullable=False, server_default="upload"),
    )
    op.add_column("resumes", sa.Column("label", sa.Text(), nullable=True))
    op.add_column("resumes", sa.Column("parent_id", sa.Uuid(), nullable=True))
    op.create_index("ix_resumes_parent_id", "resumes", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_resumes_parent_id", table_name="resumes")
    op.drop_column("resumes", "parent_id")
    op.drop_column("resumes", "label")
    op.drop_column("resumes", "source")
    op.drop_constraint("uq_gate_per_user", "gate_configs", type_="unique")
    op.drop_index("ix_gate_configs_user_id", table_name="gate_configs")
    op.drop_table("gate_configs")
