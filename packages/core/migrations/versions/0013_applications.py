"""applications: what was actually sent, and which CV went with it.

Stage 4 of the apply agent, and the first table that records the user's own action rather
than the system's proposal. Everything upstream — qualifications, drafts, plans — is
JobPilot saying what it *would* do; a row here means a form was submitted.

Three shapes of decision are baked into the DDL and worth stating once:

``job_id`` is ``ON DELETE SET NULL`` rather than ``CASCADE``, which is the opposite of
every other job-owned table. The Concierge can delete every disqualified job in one call,
and a user who applied to a role in March, watched it drop below threshold after a
qualifier re-tune, and then tidied up should not silently lose the record of having
applied. ``company`` and ``job_title`` are snapshotted at write time so the orphaned row
still answers "who did I apply to?". The same reasoning gives ``cv_draft_id`` SET NULL
beside a ``cv_label`` snapshot: drafts are deletable from the Tailored CVs list.

The unique constraint on ``(user_id, job_id)`` is what makes the write idempotent. The
extension records once per *step* of a multi-step form (Workday routinely has five), so
without it a single application would land as five rows. NULL ``job_id`` never collides
under that constraint, which is harmless because the write path refuses to insert a row
without a job in the first place.

``ix_applications_company`` supports the question this table was built for: "have I
already applied to this company, and with which CV?"

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("employer_slug", sa.Text(), nullable=True),
        sa.Column("cv_source", sa.String(16), nullable=False, server_default="none"),
        sa.Column(
            "cv_draft_id",
            sa.Uuid(),
            sa.ForeignKey("cv_drafts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("cv_label", sa.Text(), nullable=True),
        sa.Column("answers_recorded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(16), nullable=False, server_default="extension"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_applications_user_id", "applications", ["user_id"])
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_company", "applications", ["user_id", "company"])
    op.create_unique_constraint(
        "uq_application_per_user_job", "applications", ["user_id", "job_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_application_per_user_job", "applications", type_="unique")
    op.drop_index("ix_applications_company", table_name="applications")
    op.drop_index("ix_applications_job_id", table_name="applications")
    op.drop_index("ix_applications_user_id", table_name="applications")
    op.drop_table("applications")
