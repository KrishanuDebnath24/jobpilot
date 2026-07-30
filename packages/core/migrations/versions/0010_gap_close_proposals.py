"""suggestions: gap_close_json — the attested-CV proposal set, stored beside the honest one.

The Gap-closer agent proposes without a truth constraint (gap skills the résumé never
mentions, aggressive bullet rewrites, brand-new bullets), and the candidate accepts/edits/
rejects each item; accepted items ride ``cv_fields.USER_CONFIRMED`` into the alignment
loop. That set needs its own column rather than a nest inside ``projects_json`` for two
reasons:

* the Enhancer's ``_split`` rewrites ``projects_json`` and ``bullet_rewrites_json``
  wholesale on every enhance run, so a nested proposal set would be silently wiped the
  next time the user hit "Analyze & suggest";
* per-item accept state must not touch the row's ``status``, which belongs to the honest
  buckets and drives ``resume_apply.apply_suggestions`` — keeping them separate is what
  leaves apply-to-resume and the Prepare-CVs chain behaviourally unchanged.

Nullable and additive: existing rows stay NULL and read as "no proposals generated".

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("suggestions", sa.Column("gap_close_json", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("suggestions", "gap_close_json")
