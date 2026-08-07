"""Pre-configured application answers: ``application_profiles`` + ``answer_bank``.

Stage 1 of the apply agent. Both tables exist to enforce one rule: a value submitted on
a job application must trace to something the user configured or explicitly confirmed —
never to a model's inference. A job application is a legal declaration, so there is no
"the agent guessed" branch anywhere downstream of these two tables.

``application_profiles`` holds the stable facts (identity, links, logistics) as explicit
typed columns, because the canonical field map matches ATS form fields onto *named* keys;
a single JSONB blob would push that mapping into string lookups with no schema. The two
exceptions are deliberately JSONB:

* ``eligibility_json`` is keyed by **jurisdiction** (``{"IE": {...}, "UK": {...}}``).
  "Authorized to work in the United States?" and "…in Ireland?" are near-identical
  strings with opposite correct answers, so eligibility can never be a single flat
  boolean and can never be resolved by text similarity.
* ``demographics_json`` is voluntary EEO data whose available options vary by employer
  and jurisdiction; every field defaults to "prefer not to say".

One row per user (unique on ``user_id``) rather than the latest-row-wins pattern used by
``search_configs``/``qualifier_configs``: a profile is a single evolving record, not a
versioned config, and a duplicate would silently split a user's answers in two.

``answer_bank`` is the learned half — questions the user has already answered, reused on
every later form. ``question_norm`` is the exact-match key; ``question_vec`` supports
near-match for ordinary questions only. ``locked`` rows are excluded from similarity
matching entirely (see ``answer_bank.LOCKED_CATEGORIES``) and resolve by exact key or
escalate to the user.

No vector index: this table is scanned per-user and holds tens to low-hundreds of rows,
where a sequential scan beats ivfflat — which would also need training rows it does not
have on an empty table.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.create_table(
        "application_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        # --- identity (seeded from the parsed résumé's contact block) ---
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        # --- links ---
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("github_url", sa.Text(), nullable=True),
        sa.Column("portfolio_url", sa.Text(), nullable=True),
        # --- logistics ---
        sa.Column("notice_period_days", sa.Integer(), nullable=True),
        sa.Column("earliest_start_date", sa.Date(), nullable=True),
        sa.Column("salary_expectation", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.String(3), nullable=True),
        sa.Column("willing_to_relocate", sa.Boolean(), nullable=True),
        sa.Column("work_mode_preference", sa.Text(), nullable=True),
        # --- locked categories (never similarity-matched) ---
        sa.Column("eligibility_json", JSONB(), nullable=False, server_default="{}"),
        sa.Column("demographics_json", JSONB(), nullable=False, server_default="{}"),
        # --- provenance: which résumé upload seeded this, for re-seed prompts ---
        sa.Column("seeded_from_resume_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_application_profiles_user_id", "application_profiles", ["user_id"])
    op.create_unique_constraint(
        "uq_application_profile_per_user", "application_profiles", ["user_id"]
    )

    op.create_table(
        "answer_bank",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("question_norm", sa.Text(), nullable=False),
        sa.Column("question_vec", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("answer_value", sa.Text(), nullable=False),
        sa.Column("answer_kind", sa.String(16), nullable=False, server_default="text"),
        # "global" or "employer:<slug>" — "why this company?" must never be global.
        sa.Column("scope", sa.Text(), nullable=False, server_default="global"),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("times_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_answer_bank_user_id", "answer_bank", ["user_id"])
    # Composite: every exact-match lookup is (user, normalised question, scope).
    op.create_index("ix_answer_bank_lookup", "answer_bank", ["user_id", "question_norm", "scope"])
    op.create_unique_constraint(
        "uq_answer_bank_question_per_scope", "answer_bank", ["user_id", "question_norm", "scope"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_answer_bank_question_per_scope", "answer_bank", type_="unique")
    op.drop_index("ix_answer_bank_lookup", table_name="answer_bank")
    op.drop_index("ix_answer_bank_user_id", table_name="answer_bank")
    op.drop_table("answer_bank")

    op.drop_constraint("uq_application_profile_per_user", "application_profiles", type_="unique")
    op.drop_index("ix_application_profiles_user_id", table_name="application_profiles")
    op.drop_table("application_profiles")
