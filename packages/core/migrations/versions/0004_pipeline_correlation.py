"""pipeline_runs: add pipeline_id (correlate stages of one auto-run) + a partial
unique index enforcing one active 'pipeline' umbrella row per user.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Correlation id = the LangGraph thread_id. Stages of one autonomous run share it.
    op.add_column(
        "pipeline_runs",
        sa.Column("pipeline_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_pipeline_runs_pipeline_id", "pipeline_runs", ["pipeline_id"]
    )
    # Hard concurrency guard: at most one *active* umbrella run per user. The
    # orchestrator writes a stage='pipeline' row per execution; this partial unique
    # index makes a second concurrent open fail at the DB level (caught + surfaced
    # as "already running"). Stage rows (scout/qualify) are unaffected.
    op.create_index(
        "uq_active_pipeline_per_user",
        "pipeline_runs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("stage = 'pipeline' AND status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_active_pipeline_per_user", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_pipeline_id", table_name="pipeline_runs")
    op.drop_column("pipeline_runs", "pipeline_id")
