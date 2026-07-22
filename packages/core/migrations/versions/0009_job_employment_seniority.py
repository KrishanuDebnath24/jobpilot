"""jobs: employment_type + seniority (deterministic classification for filtering).

Two nullable columns Scout populates in ``normalize_raw`` — deterministically, no LLM —
from the connectors' structured fields (Adzuna ``contract_type``/``contract_time``,
JSearch ``job_employment_type``, The Muse ``levels``) with a title-keyword fallback so
crawler/ATS rows that don't carry the field are still classified. They power the
"Contract only / Freshers only" filters on ``get_jobs`` and in the jobs UI. Nullable:
when a source is silent and the title gives no hint, the value stays NULL (never guessed).
Existing rows stay NULL until re-scouted.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("employment_type", sa.String(16), nullable=True))
    op.add_column("jobs", sa.Column("seniority", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "seniority")
    op.drop_column("jobs", "employment_type")
