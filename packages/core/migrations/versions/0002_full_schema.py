"""pgvector extension + full core schema

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from pgvector.sqlalchemy import Vector

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024


def _created():
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _user_fk():
    return sa.Column(
        "user_id",
        sa.Uuid(),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )


def _empty_text_array():
    return sa.Column(ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]"))


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "resumes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("file_ref", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("parsed_json", JSONB(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
        _created(),
    )
    op.create_index("ix_resumes_user_id", "resumes", ["user_id"])

    op.create_table(
        "search_configs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("roles", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("locations", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("sites", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("keywords", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("schedule", sa.Text(), nullable=True),
        _created(),
    )
    op.create_index("ix_search_configs_user_id", "search_configs", ["user_id"])

    op.create_table(
        "qualifier_configs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("weights_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("threshold", sa.Float(), nullable=False, server_default=sa.text("0.5")),
        sa.Column("must_have", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("nice_to_have", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        _created(),
    )
    op.create_index("ix_qualifier_configs_user_id", "qualifier_configs", ["user_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=16), nullable=True),
        sa.Column("connector", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("raw_json", JSONB(), nullable=True),
        sa.Column("normalized_json", JSONB(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("dedupe_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'scouted'")),
        _created(),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    op.create_index("ix_jobs_dedupe_hash", "jobs", ["dedupe_hash"])

    op.create_table(
        "qualifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("reasons_json", JSONB(), nullable=True),
        sa.Column("matched_skills", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("missing_skills", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        _created(),
    )
    op.create_index("ix_qualifications_user_id", "qualifications", ["user_id"])
    op.create_index("ix_qualifications_job_id", "qualifications", ["job_id"])

    op.create_table(
        "analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("extracted_json", JSONB(), nullable=True),
        sa.Column("gaps_json", JSONB(), nullable=True),
        sa.Column("seniority", sa.Text(), nullable=True),
        _created(),
    )
    op.create_index("ix_analyses_user_id", "analyses", ["user_id"])
    op.create_index("ix_analyses_job_id", "analyses", ["job_id"])
    op.create_index("ix_analyses_batch_id", "analyses", ["batch_id"])

    op.create_table(
        "suggestions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("skills_to_add", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("projects_json", JSONB(), nullable=True),
        sa.Column("bullet_rewrites_json", JSONB(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'proposed'")),
        _created(),
    )
    op.create_index("ix_suggestions_user_id", "suggestions", ["user_id"])
    op.create_index("ix_suggestions_job_id", "suggestions", ["job_id"])
    op.create_index("ix_suggestions_batch_id", "suggestions", ["batch_id"])

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_ref", sa.Text(), nullable=True),
        sa.Column("output_ref", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _created(),
    )
    op.create_index("ix_pipeline_runs_user_id", "pipeline_runs", ["user_id"])

    op.create_table(
        "agent_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", JSONB(), nullable=True),
        _created(),
    )
    op.create_index("ix_agent_events_user_id", "agent_events", ["user_id"])

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("title", sa.Text(), nullable=True),
        _created(),
    )
    op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _user_fk(),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_calls_json", JSONB(), nullable=True),
        _created(),
    )
    op.create_index("ix_messages_user_id", "messages", ["user_id"])
    op.create_index("ix_messages_session_id", "messages", ["session_id"])


def downgrade() -> None:
    for tbl in [
        "messages",
        "chat_sessions",
        "agent_events",
        "pipeline_runs",
        "suggestions",
        "analyses",
        "qualifications",
        "jobs",
        "qualifier_configs",
        "search_configs",
        "resumes",
    ]:
        op.drop_table(tbl)
    op.execute("DROP EXTENSION IF EXISTS vector")
