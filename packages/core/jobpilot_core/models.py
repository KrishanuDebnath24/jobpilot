"""SQLAlchemy 2.0 ORM models for the full JobPilot data model (master §9).

Every user-owned table carries an indexed ``user_id`` FK with ``ON DELETE CASCADE``
so ``delete_user_data`` can hard-delete a user's entire footprint by removing the
single ``users`` row. ``embedding`` vector columns are defined now but left NULL
until semantic search lands in Phase 3.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Fixed at column-creation time; changing this requires a new migration.
EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _user_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    file_ref: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created()


class SearchConfig(Base):
    __tablename__ = "search_configs"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    locations: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    sites: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # Phase 2: which connectors are enabled + ATS employers to poll.
    sources: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    target_employers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    schedule: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()


class QualifierConfig(Base):
    __tablename__ = "qualifier_configs"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    weights_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    must_have: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    nice_to_have: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    created_at: Mapped[datetime] = _created()


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # api|ats|crawl|seed
    connector: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    company: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    normalized_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    dedupe_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scouted")
    created_at: Mapped[datetime] = _created()


class Qualification(Base):
    __tablename__ = "qualifications"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)  # pass|fail
    reasons_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    matched_skills: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    missing_skills: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    created_at: Mapped[datetime] = _created()


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    extracted_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    gaps_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    seniority: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()


class Suggestion(Base):
    __tablename__ = "suggestions"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    skills_to_add: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    projects_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    bullet_rewrites_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    created_at: Mapped[datetime] = _created()


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    # Correlation id (= LangGraph thread_id): stages of one autonomous run share it.
    # A row with stage='pipeline' is the per-execution "umbrella"; scout/qualify rows
    # carry the same pipeline_id so the UI can group "Run X: Scout ✓ → Qualify ⏳".
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created()


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created()


# Ordered child->parent for reference; erasure relies on ON DELETE CASCADE from users.
USER_OWNED_TABLES = [
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
]
