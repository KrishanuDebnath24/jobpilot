"""Phase 6.5: tailored CV drafts.

``cv_drafts`` holds a tailored CV per (user, job): the honestly-tailored content, the
locked-field snapshot it must match, the critic's notes, before/after re-qualification
scores, and refs to the rendered PDF/DOCX. User-owned (cascade erasure); one draft per
(user, job) — re-tailoring replaces it.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cv_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("base_resume_version", sa.Uuid(), nullable=True),
        sa.Column("tailored_content_json", JSONB(), nullable=True),
        sa.Column("locked_snapshot_json", JSONB(), nullable=True),
        sa.Column("critic_notes_json", JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft_ready"),
        sa.Column("before_score", sa.Float(), nullable=True),
        sa.Column("after_score", sa.Float(), nullable=True),
        sa.Column("pdf_ref", sa.Text(), nullable=True),
        sa.Column("docx_ref", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cv_drafts_user_id", "cv_drafts", ["user_id"])
    op.create_index("ix_cv_drafts_job_id", "cv_drafts", ["job_id"])
    op.create_unique_constraint("uq_cv_draft_per_user_job", "cv_drafts", ["user_id", "job_id"])


def downgrade() -> None:
    op.drop_constraint("uq_cv_draft_per_user_job", "cv_drafts", type_="unique")
    op.drop_index("ix_cv_drafts_job_id", table_name="cv_drafts")
    op.drop_index("ix_cv_drafts_user_id", table_name="cv_drafts")
    op.drop_table("cv_drafts")
