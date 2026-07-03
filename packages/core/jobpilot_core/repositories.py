"""Data-access functions. EVERY query is scoped by ``user_id``, which callers must
pass from the trusted request context (see mcp_auth). No function here trusts a
user_id embedded in caller-supplied payloads.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .schemas import JobIn

# --------------------------------------------------------------------------- #
# Serializers (JSON-safe dicts for MCP responses)
# --------------------------------------------------------------------------- #
def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def job_to_dict(job: models.Job) -> dict:
    return {
        "id": str(job.id),
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.url,
        "description": job.description,
        "source": job.source,
        "source_type": job.source_type,
        "connector": job.connector,
        "dedupe_hash": job.dedupe_hash,
        "status": job.status,
        "created_at": _iso(job.created_at),
    }


def resume_to_dict(resume: models.Resume) -> dict:
    return {
        "id": str(resume.id),
        "file_ref": resume.file_ref,
        "parsed_json": resume.parsed_json,
        "consent_at": _iso(resume.consent_at),
        "retention_expires_at": _iso(resume.retention_expires_at),
        "has_embedding": resume.embedding is not None,
        "created_at": _iso(resume.created_at),
    }


# --------------------------------------------------------------------------- #
# Users (auth path; backend-only)
# --------------------------------------------------------------------------- #
async def get_user_by_email(session: AsyncSession, email: str) -> models.User | None:
    res = await session.execute(
        select(models.User).where(func.lower(models.User.email) == email.lower())
    )
    return res.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> models.User | None:
    return await session.get(models.User, user_id)


async def create_user(session: AsyncSession, email: str, password_hash: str) -> models.User:
    user = models.User(email=email.lower(), password_hash=password_hash)
    session.add(user)
    await session.flush()
    return user


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def _dedupe_hash(user_id: uuid.UUID, data: JobIn) -> str:
    if data.dedupe_hash:
        return data.dedupe_hash
    basis = "|".join([str(user_id), data.title or "", data.company or "", data.url or ""])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


async def upsert_jobs(
    session: AsyncSession, user_id: uuid.UUID, jobs: list[JobIn]
) -> list[models.Job]:
    saved: list[models.Job] = []
    for data in jobs:
        dedupe = _dedupe_hash(user_id, data)
        existing = (
            await session.execute(
                select(models.Job).where(
                    models.Job.user_id == user_id, models.Job.dedupe_hash == dedupe
                )
            )
        ).scalar_one_or_none()
        target = existing or models.Job(user_id=user_id, dedupe_hash=dedupe)
        target.title = data.title
        target.company = data.company
        target.location = data.location
        target.description = data.description
        target.url = data.url
        target.source = data.source
        target.source_type = data.source_type
        target.connector = data.connector
        target.raw_json = data.raw_json
        target.normalized_json = data.normalized_json
        target.status = data.status
        if existing is None:
            session.add(target)
        saved.append(target)
    await session.flush()
    return saved


async def get_jobs(
    session: AsyncSession,
    user_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
) -> list[models.Job]:
    stmt = select(models.Job).where(models.Job.user_id == user_id)
    if status:
        stmt = stmt.where(models.Job.status == status)
    stmt = stmt.order_by(models.Job.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def get_job(
    session: AsyncSession, user_id: uuid.UUID, job_id: uuid.UUID
) -> models.Job | None:
    return (
        await session.execute(
            select(models.Job).where(
                models.Job.user_id == user_id, models.Job.id == job_id
            )
        )
    ).scalar_one_or_none()


async def search_jobs(
    session: AsyncSession, user_id: uuid.UUID, query: str, limit: int = 20
) -> list[models.Job]:
    """Phase-1 keyword fallback; semantic (embedding) path lands in Phase 3."""
    like = f"%{query}%"
    stmt = (
        select(models.Job)
        .where(
            models.Job.user_id == user_id,
            or_(
                models.Job.title.ilike(like),
                models.Job.company.ilike(like),
                models.Job.description.ilike(like),
            ),
        )
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- #
# Resumes
# --------------------------------------------------------------------------- #
async def create_resume(
    session: AsyncSession,
    user_id: uuid.UUID,
    file_ref: str,
    raw_text: str,
    parsed_json: dict,
    consent_at: datetime,
    retention_expires_at: datetime,
) -> models.Resume:
    resume = models.Resume(
        user_id=user_id,
        file_ref=file_ref,
        raw_text=raw_text,
        parsed_json=parsed_json,
        consent_at=consent_at,
        retention_expires_at=retention_expires_at,
    )
    session.add(resume)
    await session.flush()
    return resume


async def get_latest_resume(
    session: AsyncSession, user_id: uuid.UUID
) -> models.Resume | None:
    return (
        await session.execute(
            select(models.Resume)
            .where(models.Resume.user_id == user_id)
            .order_by(models.Resume.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_resume_file_refs(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    res = await session.execute(
        select(models.Resume.file_ref).where(models.Resume.user_id == user_id)
    )
    return [r for (r,) in res.all()]


# --------------------------------------------------------------------------- #
# Qualifications / analyses / suggestions
# --------------------------------------------------------------------------- #
async def get_qualifications(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    res = await session.execute(
        select(models.Qualification).where(models.Qualification.user_id == user_id)
    )
    return [
        {
            "id": str(q.id),
            "job_id": str(q.job_id),
            "score": q.score,
            "decision": q.decision,
            "matched_skills": q.matched_skills,
            "missing_skills": q.missing_skills,
        }
        for q in res.scalars().all()
    ]


async def get_analyses(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    res = await session.execute(
        select(models.Analysis).where(models.Analysis.user_id == user_id)
    )
    return [
        {"id": str(a.id), "job_id": str(a.job_id) if a.job_id else None, "seniority": a.seniority}
        for a in res.scalars().all()
    ]


async def get_suggestions(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    res = await session.execute(
        select(models.Suggestion).where(models.Suggestion.user_id == user_id)
    )
    return [
        {
            "id": str(s.id),
            "job_id": str(s.job_id) if s.job_id else None,
            "skills_to_add": s.skills_to_add,
            "status": s.status,
        }
        for s in res.scalars().all()
    ]


async def update_suggestion_status(
    session: AsyncSession, user_id: uuid.UUID, suggestion_id: uuid.UUID, status: str
) -> models.Suggestion | None:
    suggestion = (
        await session.execute(
            select(models.Suggestion).where(
                models.Suggestion.user_id == user_id,
                models.Suggestion.id == suggestion_id,
            )
        )
    ).scalar_one_or_none()
    if suggestion is not None:
        suggestion.status = status
        await session.flush()
    return suggestion


# --------------------------------------------------------------------------- #
# Erasure (GDPR) — hard delete + verification helpers
# --------------------------------------------------------------------------- #
async def delete_user_cascade(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Delete the users row; every user-owned table cascades via ON DELETE CASCADE."""
    await session.execute(delete(models.User).where(models.User.id == user_id))


async def count_user_rows(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Count remaining rows keyed to the user across every user-owned table (+ users)."""
    counts: dict[str, int] = {}
    for table in models.USER_OWNED_TABLES:
        res = await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        counts[table] = int(res.scalar_one())
    res = await session.execute(
        text("SELECT count(*) FROM users WHERE id = :uid"), {"uid": str(user_id)}
    )
    counts["users"] = int(res.scalar_one())
    return counts
