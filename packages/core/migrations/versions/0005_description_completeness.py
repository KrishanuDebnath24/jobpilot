"""jobs: description completeness flag (Addendum 2.2).

``description_completeness`` (full | snippet) records whether a job's ``description``
is usable full text or only an aggregator teaser. Connectors set it by source
(ATS/crawl/JSearch -> full; Adzuna/Jooble/Careerjet -> snippet). Consumers treat
``snippet`` as low-confidence — the Qualifier marks such results ``limited_data``.
Existing rows are backfilled from ``source_type``.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "description_completeness",
            sa.String(16),
            nullable=False,
            server_default="snippet",
        ),
    )
    # Backfill: direct sources (ATS/crawler) carry full descriptions; aggregator (api)
    # rows are snippets. New rows are tagged by their connector.
    op.execute(
        "UPDATE jobs SET description_completeness = 'full' "
        "WHERE source_type IN ('ats', 'crawl')"
    )


def downgrade() -> None:
    op.drop_column("jobs", "description_completeness")
