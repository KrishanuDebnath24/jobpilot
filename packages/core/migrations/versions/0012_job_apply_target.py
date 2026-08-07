"""jobs: where the application form actually lives.

Stage 2 of the apply agent. Five of the seven connectors store a *tracking redirect* in
``jobs.url`` rather than an application form — Adzuna gives ``redirect_url``, Careerjet
and Jooble their own hop, The Muse its landing page. Following that chain lands on one of
a small set of applicant tracking systems, and that terminal URL is what any later stage
has to work against. These columns cache the answer so the hop is paid once per job
instead of on every run.

``apply_resolved_from`` exists because ``upsert_jobs`` may legitimately rewrite
``jobs.url``: when a higher-precedence connector reports the same job (a direct ATS
record beating an aggregator copy), the canonical link changes underneath us. Storing the
URL that was actually followed makes staleness a comparison — ``url != apply_resolved_from``
means re-resolve — instead of something we would otherwise have to guess at from timestamps.

``apply_ats`` holds a detector id (``greenhouse``, ``lever``, ``workday``, …), the literal
``unknown`` when the terminal host matches nothing known, or ``aggregator`` when the chain
never left a job board — which is a real outcome worth distinguishing from "not yet tried",
since it means there is no reachable form behind that listing.

All nullable and additive: existing rows read as "not yet resolved", and nothing in the
scout/qualify path touches these columns.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("apply_url", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("apply_ats", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("apply_board_slug", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("apply_resolved_from", sa.Text(), nullable=True))
    op.add_column(
        "jobs", sa.Column("apply_resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Partial index: the dashboard filters "which jobs are reachable, and via what",
    # which only ever scans resolved rows.
    op.create_index(
        "ix_jobs_apply_ats",
        "jobs",
        ["user_id", "apply_ats"],
        postgresql_where=sa.text("apply_ats IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_apply_ats", table_name="jobs")
    op.drop_column("jobs", "apply_resolved_at")
    op.drop_column("jobs", "apply_resolved_from")
    op.drop_column("jobs", "apply_board_slug")
    op.drop_column("jobs", "apply_ats")
    op.drop_column("jobs", "apply_url")
