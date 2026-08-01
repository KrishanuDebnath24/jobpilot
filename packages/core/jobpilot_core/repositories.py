"""Data-access functions. EVERY query is scoped by ``user_id``, which callers must
pass from the trusted request context (see mcp_auth). No function here trusts a
user_id embedded in caller-supplied payloads.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from . import models
from .gates import gate_stage  # gate rows are pipeline_runs with a 'gate:<key>' stage
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
        "description_completeness": job.description_completeness,
        "employment_type": job.employment_type,
        "seniority": job.seniority,
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
        "source": resume.source,
        "label": resume.label,
        "parent_id": str(resume.parent_id) if resume.parent_id else None,
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
    # Scout supplies its own hash (company+title+location); this fallback matches
    # that basis for any direct upsert that omits one.
    from .scout import compute_dedupe_hash

    return compute_dedupe_hash(data.company, data.title, data.location, data.url)


async def upsert_jobs(
    session: AsyncSession, user_id: uuid.UUID, jobs: list[JobIn]
) -> list[models.Job]:
    """Idempotent upsert keyed by ``(user_id, dedupe_hash)``. Collapses duplicates
    both against existing rows AND within this batch. On a collision, record precedence
    decides which record's fields survive — ``(source tier, completeness)`` compared as
    a tuple: a *direct* source (ats > crawl) beats an *aggregator* (api), and WITHIN a
    tier a ``full`` record beats a ``snippet`` (Addendum 2.2 revised). So a shared job
    keeps its canonical apply link, richer fields, and full text regardless of connector
    run order, and the survivor's ``description_completeness`` reflects the winner."""
    from .scout import record_precedence

    saved: list[models.Job] = []
    batch: dict[str, models.Job] = {}  # dedupe_hash -> row created/updated in this call
    for data in jobs:
        dedupe = _dedupe_hash(user_id, data)
        if dedupe in batch:
            target, is_new = batch[dedupe], False  # intra-batch duplicate -> reuse
        else:
            existing = (
                await session.execute(
                    select(models.Job).where(
                        models.Job.user_id == user_id, models.Job.dedupe_hash == dedupe
                    )
                )
            ).scalar_one_or_none()
            target = existing or models.Job(user_id=user_id, dedupe_hash=dedupe)
            is_new = existing is None
            if is_new:
                session.add(target)
            batch[dedupe] = target
            saved.append(target)  # one entry per unique dedupe_hash (new or updated)
        # Overwrite only when the incoming record's precedence is at least as high as
        # what's there: a lower-tier aggregator copy never clobbers a direct record, and
        # within a tier a snippet never clobbers full text (full wins the tiebreak; equal
        # completeness keeps last-writer-wins).
        incoming = record_precedence(data.source_type, data.description_completeness)
        current = record_precedence(target.source_type, target.description_completeness)
        if is_new or incoming >= current:
            target.title = data.title
            target.company = data.company
            target.location = data.location
            target.description = data.description
            target.url = data.url
            target.source = data.source
            target.source_type = data.source_type
            target.connector = data.connector
            target.description_completeness = data.description_completeness
            target.employment_type = data.employment_type
            target.seniority = data.seniority
            target.raw_json = data.raw_json
            target.normalized_json = data.normalized_json
            target.status = data.status
    await session.flush()
    return saved


async def hydrate_snippet_jobs(session: AsyncSession, user_id: uuid.UUID) -> dict:
    """Level 2 cross-source hydration: give a ``snippet`` job the full-text body of a
    ``full``/``hydrated`` job that is confidently the same posting (same employer +
    location + role), when Level 1's dedupe hash didn't already collapse them. Copies
    the description and flips ``description_completeness`` to ``hydrated``; it never
    merges or deletes rows, and only ever touches snippet rows. Deterministic (no LLM);
    the confident-match rule lives in :func:`scout.plan_hydration`. Returns
    ``{"hydrated": n, "details": [...]}``."""
    from .scout import plan_hydration

    rows = (
        await session.execute(select(models.Job).where(models.Job.user_id == user_id))
    ).scalars().all()
    by_id = {str(r.id): r for r in rows}
    jobs = [
        {
            "id": str(r.id), "company": r.company, "title": r.title, "location": r.location,
            "description": r.description, "description_completeness": r.description_completeness,
        }
        for r in rows
    ]
    details: list[dict] = []
    for plan in plan_hydration(jobs):
        row = by_id.get(plan["snippet_id"])
        if row is None or row.description_completeness != "snippet":
            continue  # guard: only ever hydrate a still-snippet row
        row.description = plan["description"]
        row.description_completeness = "hydrated"
        details.append({"job_id": plan["snippet_id"], "source_id": plan["source_id"]})
    if details:
        await session.flush()
    return {"hydrated": len(details), "details": details}


async def get_jobs(
    session: AsyncSession,
    user_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
    employment_type: str | None = None,
    seniority: str | None = None,
) -> list[models.Job]:
    stmt = select(models.Job).where(models.Job.user_id == user_id)
    if status:
        stmt = stmt.where(models.Job.status == status)
    if employment_type:
        stmt = stmt.where(models.Job.employment_type == employment_type)
    if seniority:
        stmt = stmt.where(models.Job.seniority == seniority)
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


async def get_latest_uploaded_resume(
    session: AsyncSession, user_id: uuid.UUID
) -> models.Resume | None:
    """The user's most recent *uploaded* resume (``source='upload'``). Assembled
    versions have a synthetic ``file_ref`` and no stored file, so they're excluded —
    this is the row whose original PDF/DOCX we can stream back."""
    return (
        await session.execute(
            select(models.Resume)
            .where(models.Resume.user_id == user_id, models.Resume.source == "upload")
            .order_by(models.Resume.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_resume_file_refs(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    res = await session.execute(
        select(models.Resume.file_ref).where(models.Resume.user_id == user_id)
    )
    return [r for (r,) in res.all()]


async def create_resume_version(
    session: AsyncSession,
    user_id: uuid.UUID,
    parsed_json: dict,
    raw_text: str,
    label: str | None,
    parent_id: uuid.UUID | None,
    consent_at: datetime | None,
    retention_expires_at: datetime | None,
) -> models.Resume:
    """Persist an *assembled* resume version (Phase 6, 6e). It's a new row — the
    original is preserved. ``file_ref`` is a synthetic marker (no uploaded file)."""
    resume = models.Resume(
        user_id=user_id,
        file_ref=f"assembled://{uuid.uuid4()}",
        raw_text=raw_text,
        parsed_json=parsed_json,
        source="assembled",
        label=label,
        parent_id=parent_id,
        consent_at=consent_at,
        retention_expires_at=retention_expires_at,
    )
    session.add(resume)
    await session.flush()
    return resume


async def delete_assembled_resumes(
    session: AsyncSession, user_id: uuid.UUID, keep_id: uuid.UUID | None = None
) -> int:
    """Delete the user's *assembled* resume versions, optionally keeping one.

    Uploaded resumes are never touched — the filter is ``source == 'assembled'``, so the
    working resume (and every file the user actually gave us) is out of reach of this call
    by construction, not by the caller remembering to be careful. Assembled rows carry a
    synthetic ``file_ref`` and no stored file, so there is nothing to clean up in storage.
    Scoped to ``user_id``. Returns rows deleted."""
    stmt = delete(models.Resume).where(
        models.Resume.user_id == user_id, models.Resume.source == "assembled"
    )
    if keep_id is not None:
        stmt = stmt.where(models.Resume.id != keep_id)
    res = await session.execute(stmt)
    return int(res.rowcount or 0)


async def list_resumes(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    """All of a user's resume versions (upload + assembled), newest first."""
    res = await session.execute(
        select(models.Resume)
        .where(models.Resume.user_id == user_id)
        .order_by(models.Resume.created_at.desc())
    )
    return [resume_to_dict(r) for r in res.scalars().all()]


async def get_resume(
    session: AsyncSession, user_id: uuid.UUID, resume_id: uuid.UUID
) -> models.Resume | None:
    return (
        await session.execute(
            select(models.Resume).where(
                models.Resume.user_id == user_id, models.Resume.id == resume_id
            )
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Gate config (Phase 6) — per-user human-in-the-loop toggles
# --------------------------------------------------------------------------- #
async def get_gate_configs(session: AsyncSession, user_id: uuid.UUID) -> dict[str, bool]:
    """Return the user's stored gate overrides as ``{gate_key: enabled}`` (defaults
    are applied by ``jobpilot_core.gates.effective_gates`` at the read boundary)."""
    res = await session.execute(
        select(models.GateConfig).where(models.GateConfig.user_id == user_id)
    )
    return {g.gate_key: g.enabled for g in res.scalars().all()}


async def upsert_gate_config(
    session: AsyncSession, user_id: uuid.UUID, gate_key: str, enabled: bool
) -> models.GateConfig:
    existing = (
        await session.execute(
            select(models.GateConfig).where(
                models.GateConfig.user_id == user_id, models.GateConfig.gate_key == gate_key
            )
        )
    ).scalar_one_or_none()
    row = existing or models.GateConfig(user_id=user_id, gate_key=gate_key)
    row.enabled = enabled
    if existing is None:
        session.add(row)
    await session.flush()
    return row


# --------------------------------------------------------------------------- #
# Search config (config-mcp)
# --------------------------------------------------------------------------- #
_SEARCH_CONFIG_FIELDS = (
    "roles",
    "locations",
    "keywords",
    "sites",
    "sources",
    "target_employers",
    "schedule",
)


def search_config_to_dict(cfg: models.SearchConfig | None) -> dict:
    if cfg is None:
        return {
            "roles": [], "locations": [], "keywords": [], "sites": [],
            "sources": [], "target_employers": [], "schedule": None,
        }
    return {
        "id": str(cfg.id),
        "roles": list(cfg.roles or []),
        "locations": list(cfg.locations or []),
        "keywords": list(cfg.keywords or []),
        "sites": list(cfg.sites or []),
        "sources": list(cfg.sources or []),
        "target_employers": list(cfg.target_employers or []),
        "schedule": cfg.schedule,
        "created_at": _iso(cfg.created_at),
    }


async def get_search_config(
    session: AsyncSession, user_id: uuid.UUID
) -> models.SearchConfig | None:
    return (
        await session.execute(
            select(models.SearchConfig)
            .where(models.SearchConfig.user_id == user_id)
            .order_by(models.SearchConfig.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def upsert_search_config(
    session: AsyncSession, user_id: uuid.UUID, fields: dict
) -> models.SearchConfig:
    """Create-or-update the user's single search config. Only keys present in
    ``fields`` are written (partial update); everything else is left untouched."""
    cfg = await get_search_config(session, user_id)
    if cfg is None:
        cfg = models.SearchConfig(user_id=user_id)
        session.add(cfg)
    for key in _SEARCH_CONFIG_FIELDS:
        if key in fields and fields[key] is not None:
            setattr(cfg, key, fields[key])
    await session.flush()
    return cfg


# --------------------------------------------------------------------------- #
# Qualifier config (config-mcp)
# --------------------------------------------------------------------------- #
_QUALIFIER_FIELDS = ("weights_json", "threshold", "must_have", "nice_to_have")


def qualifier_config_to_dict(cfg: models.QualifierConfig | None) -> dict:
    from .qualifier import DEFAULT_THRESHOLD, DEFAULT_WEIGHTS

    if cfg is None:
        return {
            "id": None, "weights_json": dict(DEFAULT_WEIGHTS), "threshold": DEFAULT_THRESHOLD,
            "must_have": [], "nice_to_have": [],
        }
    return {
        "id": str(cfg.id),
        "weights_json": cfg.weights_json or {},
        "threshold": cfg.threshold,
        "must_have": list(cfg.must_have or []),
        "nice_to_have": list(cfg.nice_to_have or []),
        "created_at": _iso(cfg.created_at),
    }


async def get_qualifier_config(
    session: AsyncSession, user_id: uuid.UUID
) -> models.QualifierConfig | None:
    return (
        await session.execute(
            select(models.QualifierConfig)
            .where(models.QualifierConfig.user_id == user_id)
            .order_by(models.QualifierConfig.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def upsert_qualifier_config(
    session: AsyncSession, user_id: uuid.UUID, fields: dict
) -> models.QualifierConfig:
    cfg = await get_qualifier_config(session, user_id)
    if cfg is None:
        cfg = models.QualifierConfig(user_id=user_id)
        session.add(cfg)
    for key in _QUALIFIER_FIELDS:
        if key in fields and fields[key] is not None:
            setattr(cfg, key, fields[key])
    await session.flush()
    return cfg


# --------------------------------------------------------------------------- #
# Qualifications / analyses / suggestions
# --------------------------------------------------------------------------- #
async def upsert_qualification(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    score: float | None,
    decision: str | None,
    reasons_json: dict | None,
    matched_skills: list[str],
    missing_skills: list[str],
) -> models.Qualification:
    """One qualification per (user, job); re-qualify overwrites (idempotent)."""
    existing = (
        await session.execute(
            select(models.Qualification).where(
                models.Qualification.user_id == user_id,
                models.Qualification.job_id == job_id,
            )
        )
    ).scalar_one_or_none()
    q = existing or models.Qualification(user_id=user_id, job_id=job_id)
    q.score = score
    q.decision = decision
    q.reasons_json = reasons_json
    q.matched_skills = matched_skills
    q.missing_skills = missing_skills
    if existing is None:
        session.add(q)
    await session.flush()
    return q


async def get_qualified_jobs(session: AsyncSession, user_id: uuid.UUID, limit: int = 1000) -> list[dict]:
    """Jobs joined with their qualification (LEFT JOIN) for the dashboard."""
    stmt = (
        select(models.Job, models.Qualification)
        .outerjoin(
            models.Qualification,
            (models.Qualification.job_id == models.Job.id)
            & (models.Qualification.user_id == user_id),
        )
        .where(models.Job.user_id == user_id)
        .order_by(models.Job.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    out: list[dict] = []
    for job, q in rows:
        d = job_to_dict(job)
        d["qualification"] = (
            {
                "score": q.score,
                "decision": q.decision,
                "matched_skills": list(q.matched_skills or []),
                "missing_skills": list(q.missing_skills or []),
                "reasons_json": q.reasons_json,
            }
            if q is not None
            else None
        )
        out.append(d)
    return out


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


def analysis_to_dict(a: models.Analysis) -> dict:
    return {
        "id": str(a.id),
        "job_id": str(a.job_id) if a.job_id else None,
        "batch_id": str(a.batch_id) if a.batch_id else None,
        "extracted_json": a.extracted_json,
        "gaps_json": a.gaps_json,
        "seniority": a.seniority,
        "created_at": _iso(a.created_at),
    }


def suggestion_to_dict(s: models.Suggestion) -> dict:
    return {
        "id": str(s.id),
        "job_id": str(s.job_id) if s.job_id else None,
        "batch_id": str(s.batch_id) if s.batch_id else None,
        "skills_to_add": list(s.skills_to_add or []),
        "projects_json": s.projects_json,
        "bullet_rewrites_json": s.bullet_rewrites_json,
        "gap_close_json": s.gap_close_json,
        "status": s.status,
        "created_at": _iso(s.created_at),
    }


async def upsert_analysis(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    extracted_json: dict,
    gaps_json: dict | None,
    seniority: str | None,
    batch_id: uuid.UUID | None = None,
) -> models.Analysis:
    """One analysis per (user, job); re-analyze overwrites (idempotent)."""
    existing = (
        await session.execute(
            select(models.Analysis).where(
                models.Analysis.user_id == user_id, models.Analysis.job_id == job_id
            )
        )
    ).scalar_one_or_none()
    a = existing or models.Analysis(user_id=user_id, job_id=job_id)
    a.extracted_json = extracted_json
    a.gaps_json = gaps_json
    a.seniority = seniority
    a.batch_id = batch_id
    if existing is None:
        session.add(a)
    await session.flush()
    return a


async def upsert_suggestion(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID | None,
    skills_to_add: list[str],
    projects_json: dict | None,
    bullet_rewrites_json: dict | None,
    batch_id: uuid.UUID | None = None,
) -> models.Suggestion:
    """Persist a suggestion (status resets to 'proposed' on regenerate). Job-scoped
    suggestions upsert by (user, job); the batch aggregate (job_id NULL) replaces any
    prior aggregate for the user so re-running 'analyze all' doesn't stack duplicates."""
    if job_id is not None:
        existing = (
            await session.execute(
                select(models.Suggestion).where(
                    models.Suggestion.user_id == user_id, models.Suggestion.job_id == job_id
                )
            )
        ).scalar_one_or_none()
        s = existing or models.Suggestion(user_id=user_id, job_id=job_id)
        is_new = existing is None
    else:
        await session.execute(
            delete(models.Suggestion).where(
                models.Suggestion.user_id == user_id, models.Suggestion.job_id.is_(None)
            )
        )
        s = models.Suggestion(user_id=user_id, job_id=None)
        is_new = True
    s.skills_to_add = skills_to_add
    s.projects_json = projects_json
    s.bullet_rewrites_json = bullet_rewrites_json
    s.batch_id = batch_id
    s.status = "proposed"
    if is_new:
        session.add(s)
    await session.flush()
    return s


async def upsert_gap_close(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    gap_close: dict,
) -> models.Suggestion:
    """Persist the Gap-closer's proposal set for one job (Phase 6.6), replacing any prior
    set for that job so regenerating can't stack stale items.

    Shares the (user, job) suggestions row with the honest Enhancer buckets but writes only
    ``gap_close_json`` — the honest columns and ``status`` are left exactly as they were, so
    apply-to-resume is unaffected. Creates the row if the user ran gap-close before ever
    running Analyze & suggest."""
    existing = (
        await session.execute(
            select(models.Suggestion).where(
                models.Suggestion.user_id == user_id, models.Suggestion.job_id == job_id
            )
        )
    ).scalar_one_or_none()
    s = existing or models.Suggestion(user_id=user_id, job_id=job_id, skills_to_add=[])
    s.gap_close_json = gap_close
    if existing is None:
        session.add(s)
    await session.flush()
    return s


async def set_gap_close_items(
    session: AsyncSession,
    user_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    items: list[dict],
) -> models.Suggestion | None:
    """Apply the user's per-item review to a stored proposal set.

    Each entry is ``{"kind": ..., "id": ..., "accepted": bool|None, <edited fields>}``.
    Accept state and any edited text are written **in place** on the matching item, so the
    accepted set is always self-describing — a downstream reader never has to merge an
    edit history to know what the user actually endorsed. Unknown kinds/ids are ignored
    (a stale UI can't corrupt the set), and ``status`` is deliberately untouched.
    """
    suggestion = (
        await session.execute(
            select(models.Suggestion).where(
                models.Suggestion.user_id == user_id,
                models.Suggestion.id == suggestion_id,
            )
        )
    ).scalar_one_or_none()
    if suggestion is None:
        return None

    # JSONB is mutated by replacement, not in place — SQLAlchemy won't see attribute-level
    # mutation of a nested dict, so build a new structure and reassign.
    gc = copy.deepcopy(suggestion.gap_close_json or {})
    editable = ("skill", "closes", "interview_prep", "before", "after", "text", "role_ref")
    for patch in items or []:
        if not isinstance(patch, dict):
            continue
        kind, item_id = patch.get("kind"), patch.get("id")
        bucket = gc.get(kind)
        if not isinstance(bucket, list):
            continue
        for item in bucket:
            if not isinstance(item, dict) or item.get("id") != item_id:
                continue
            if "accepted" in patch:
                item["accepted"] = patch["accepted"]
            for field in editable:
                if field in patch and patch[field] is not None:
                    item[field] = patch[field]
                    item["user_edited"] = True
            break
    suggestion.gap_close_json = gc
    await session.flush()
    return suggestion


async def get_analyses(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    res = await session.execute(
        select(models.Analysis)
        .where(models.Analysis.user_id == user_id)
        .order_by(models.Analysis.created_at.desc())
    )
    return [analysis_to_dict(a) for a in res.scalars().all()]


async def get_suggestions(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    res = await session.execute(
        select(models.Suggestion)
        .where(models.Suggestion.user_id == user_id)
        .order_by(models.Suggestion.created_at.desc())
    )
    return [suggestion_to_dict(s) for s in res.scalars().all()]


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
# Chat sessions + messages (Phase 6 Concierge) — the persisted conversation
# --------------------------------------------------------------------------- #
def message_to_dict(m: models.Message) -> dict:
    return {
        "id": str(m.id),
        "role": m.role,
        "content": m.content,
        "tool_calls_json": m.tool_calls_json,
        "created_at": _iso(m.created_at),
    }


async def create_chat_session(
    session: AsyncSession, user_id: uuid.UUID, title: str | None = None
) -> models.ChatSession:
    cs = models.ChatSession(user_id=user_id, title=title)
    session.add(cs)
    await session.flush()
    return cs


async def list_chat_sessions(session: AsyncSession, user_id: uuid.UUID, limit: int = 50) -> list[dict]:
    res = await session.execute(
        select(models.ChatSession)
        .where(models.ChatSession.user_id == user_id)
        .order_by(models.ChatSession.created_at.desc())
        .limit(limit)
    )
    return [{"id": str(c.id), "title": c.title, "created_at": _iso(c.created_at)}
            for c in res.scalars().all()]


async def append_message(
    session: AsyncSession,
    user_id: uuid.UUID,
    chat_session_id: uuid.UUID,
    role: str,
    content: str | None,
    tool_calls_json: dict | None = None,
) -> models.Message | None:
    """Append a message to a chat session the caller owns (scoped by user_id — a
    caller can never write into another user's conversation)."""
    owner = (
        await session.execute(
            select(models.ChatSession.id).where(
                models.ChatSession.id == chat_session_id,
                models.ChatSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if owner is None:
        return None
    msg = models.Message(
        user_id=user_id, session_id=chat_session_id, role=role,
        content=content, tool_calls_json=tool_calls_json,
    )
    session.add(msg)
    await session.flush()
    return msg


async def get_messages(
    session: AsyncSession, user_id: uuid.UUID, chat_session_id: uuid.UUID, limit: int = 200
) -> list[dict]:
    res = await session.execute(
        select(models.Message)
        .where(
            models.Message.user_id == user_id,
            models.Message.session_id == chat_session_id,
        )
        .order_by(models.Message.created_at.asc())
        .limit(limit)
    )
    return [message_to_dict(m) for m in res.scalars().all()]


# --------------------------------------------------------------------------- #
# Pipeline runs (Scout background-job tracking)
# --------------------------------------------------------------------------- #
def pipeline_run_to_dict(run: models.PipelineRun) -> dict:
    counts = None
    if run.output_ref:
        try:
            counts = json.loads(run.output_ref)
        except (ValueError, TypeError):
            counts = None
    return {
        "id": str(run.id),
        "pipeline_id": str(run.pipeline_id) if run.pipeline_id else None,
        "stage": run.stage,
        "status": run.status,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "counts": counts,
        "error": run.error,
        "created_at": _iso(run.created_at),
    }


# States a pipeline/stage run is in while it still occupies the concurrency guard.
ACTIVE_STATUSES = ("queued", "running")
UMBRELLA_STAGE = "pipeline"  # the per-execution correlation row


async def create_pipeline_run(
    session: AsyncSession,
    user_id: uuid.UUID,
    stage: str,
    pipeline_id: uuid.UUID | None = None,
    status: str = "running",
) -> models.PipelineRun:
    run = models.PipelineRun(
        user_id=user_id, stage=stage, status=status, pipeline_id=pipeline_id,
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    await session.flush()
    return run


async def finish_pipeline_run(
    session: AsyncSession,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
    status: str,
    output_ref: str | None = None,
    error: str | None = None,
) -> models.PipelineRun | None:
    run = (
        await session.execute(
            select(models.PipelineRun).where(
                models.PipelineRun.user_id == user_id, models.PipelineRun.id == run_id
            )
        )
    ).scalar_one_or_none()
    if run is not None:
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        run.output_ref = output_ref
        run.error = error
        await session.flush()
    return run


async def get_pipeline_runs(
    session: AsyncSession, user_id: uuid.UUID, stage: str | None = None, limit: int = 20
) -> list[dict]:
    stmt = select(models.PipelineRun).where(models.PipelineRun.user_id == user_id)
    if stage:
        stmt = stmt.where(models.PipelineRun.stage == stage)
    stmt = stmt.order_by(models.PipelineRun.created_at.desc()).limit(limit)
    return [pipeline_run_to_dict(r) for r in (await session.execute(stmt)).scalars().all()]


# --------------------------------------------------------------------------- #
# Pipeline correlation, concurrency guard, and grouping (Phase 4 orchestrator)
# --------------------------------------------------------------------------- #
async def get_stage_status(
    session: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID, stage: str
) -> dict | None:
    """The persisted status of one stage of one pipeline — the *source of truth*
    the orchestrator reconciles against (never trusts a pub/sub event alone)."""
    stmt = (
        select(models.PipelineRun)
        .where(
            models.PipelineRun.user_id == user_id,
            models.PipelineRun.pipeline_id == pipeline_id,
            models.PipelineRun.stage == stage,
        )
        .order_by(models.PipelineRun.created_at.desc())
        .limit(1)
    )
    run = (await session.execute(stmt)).scalar_one_or_none()
    return pipeline_run_to_dict(run) if run else None


# --------------------------------------------------------------------------- #
# Gate lifecycle (Phase 6) — a gate is a pipeline_runs row with stage 'gate:<key>'
# so the orchestrator reconciles a gate decision against persisted truth the same way
# it reconciles a stage (never hanging on an approval event).
# --------------------------------------------------------------------------- #
async def _latest_gate_row(
    session: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID, gate_key: str
) -> models.PipelineRun | None:
    stmt = (
        select(models.PipelineRun)
        .where(
            models.PipelineRun.user_id == user_id,
            models.PipelineRun.pipeline_id == pipeline_id,
            models.PipelineRun.stage == gate_stage(gate_key),
        )
        .order_by(models.PipelineRun.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_gate_status(
    session: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID, gate_key: str
) -> str | None:
    """The persisted status of one gate ('pending' | 'approved' | 'skipped'), or None."""
    row = await _latest_gate_row(session, user_id, pipeline_id, gate_key)
    return row.status if row else None


async def open_gate_pending(
    session: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID, gate_key: str
) -> models.PipelineRun:
    """Mark a gate pending (idempotent: a pipeline that resumes onto an already-open
    or already-resolved gate keeps the existing row)."""
    existing = await _latest_gate_row(session, user_id, pipeline_id, gate_key)
    if existing is not None:
        return existing
    return await create_pipeline_run(
        session, user_id, gate_stage(gate_key), pipeline_id=pipeline_id, status="pending"
    )


async def resolve_gate(
    session: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID, gate_key: str, decision: str
) -> models.PipelineRun:
    """Record an approve/skip decision. Upserts so an approval that races ahead of the
    gate node still lands (the node then reads it as resolved and proceeds)."""
    status = "approved" if decision == "approve" else "skipped"
    row = await _latest_gate_row(session, user_id, pipeline_id, gate_key)
    if row is None:
        row = await create_pipeline_run(
            session, user_id, gate_stage(gate_key), pipeline_id=pipeline_id, status=status
        )
    else:
        row.status = status
    row.finished_at = datetime.now(timezone.utc)
    await session.flush()
    return row


async def get_active_pipeline(
    session: AsyncSession, user_id: uuid.UUID
) -> models.PipelineRun | None:
    """The user's currently-active umbrella run (if any) — the guard anchor."""
    stmt = (
        select(models.PipelineRun)
        .where(
            models.PipelineRun.user_id == user_id,
            models.PipelineRun.stage == UMBRELLA_STAGE,
            models.PipelineRun.status.in_(ACTIVE_STATUSES),
        )
        .order_by(models.PipelineRun.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def reap_stale_pipelines(
    session: AsyncSession, user_id: uuid.UUID, ttl_seconds: int
) -> int:
    """Fail-close any umbrella that has been 'active' longer than ``ttl_seconds`` (a
    worker died mid-run). Prevents a crashed run from locking the user out forever.
    Returns the number reaped."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    stmt = select(models.PipelineRun).where(
        models.PipelineRun.user_id == user_id,
        models.PipelineRun.stage == UMBRELLA_STAGE,
        models.PipelineRun.status.in_(ACTIVE_STATUSES),
        models.PipelineRun.started_at < cutoff,
    )
    stale = (await session.execute(stmt)).scalars().all()
    for run in stale:
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.error = f"reaped: no completion within {ttl_seconds}s (worker lost?)"
    if stale:
        await session.flush()
    return len(stale)


async def open_pipeline(
    session: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID
) -> models.PipelineRun:
    """Insert the umbrella row for a new execution. The partial-unique index
    ``uq_active_pipeline_per_user`` makes a concurrent second open raise
    ``IntegrityError`` — the hard "one active pipeline per user" guard. Callers
    catch that and surface "already running"."""
    run = models.PipelineRun(
        user_id=user_id, stage=UMBRELLA_STAGE, status="running",
        pipeline_id=pipeline_id, started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    await session.flush()  # raises IntegrityError here if one is already active
    return run


async def get_pipeline(
    session: AsyncSession, user_id: uuid.UUID, pipeline_id: uuid.UUID
) -> dict | None:
    """Assemble one execution: the umbrella + its stage runs, newest-stage-wins."""
    stmt = (
        select(models.PipelineRun)
        .where(
            models.PipelineRun.user_id == user_id,
            models.PipelineRun.pipeline_id == pipeline_id,
        )
        .order_by(models.PipelineRun.created_at.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return None
    return _assemble_pipeline(pipeline_id, rows)


async def list_pipelines(
    session: AsyncSession, user_id: uuid.UUID, limit: int = 20
) -> list[dict]:
    """Recent executions (grouped by pipeline_id), newest first."""
    stmt = (
        select(models.PipelineRun)
        .where(
            models.PipelineRun.user_id == user_id,
            models.PipelineRun.pipeline_id.is_not(None),
        )
        .order_by(models.PipelineRun.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    groups: dict[uuid.UUID, list[models.PipelineRun]] = {}
    for r in rows:
        groups.setdefault(r.pipeline_id, []).append(r)
    out = [_assemble_pipeline(pid, sorted(rs, key=lambda x: x.created_at)) for pid, rs in groups.items()]
    out.sort(key=lambda p: p["created_at"] or "", reverse=True)
    return out[:limit]


def _assemble_pipeline(pipeline_id: uuid.UUID, rows: list[models.PipelineRun]) -> dict:
    """Fold an execution's rows into {pipeline_id, status, stages:{scout,qualify}}."""
    umbrella = next((r for r in rows if r.stage == UMBRELLA_STAGE), None)
    stages: dict[str, dict] = {}
    for r in rows:
        if r.stage == UMBRELLA_STAGE:
            continue
        stages[r.stage] = pipeline_run_to_dict(r)  # last one wins (rows are time-asc)
    created = umbrella.created_at if umbrella else (rows[0].created_at if rows else None)
    return {
        "pipeline_id": str(pipeline_id),
        "status": umbrella.status if umbrella else "unknown",
        "error": umbrella.error if umbrella else None,
        "created_at": _iso(created),
        "finished_at": _iso(umbrella.finished_at) if umbrella else None,
        "stages": stages,
    }


async def list_scheduled_users(session: AsyncSession) -> list[dict]:
    """SYSTEM-level read (the scheduler daemon, not a user): every user with a
    non-empty ``search_configs.schedule``, plus the timestamp of their most recent
    umbrella run (to compute due-ness). This is the one cross-user query in the
    codebase; it exists only to let the beat scheduler fire pipelines autonomously.
    """
    cfg = models.SearchConfig
    stmt = select(cfg.user_id, cfg.schedule).where(
        cfg.schedule.is_not(None), func.length(func.trim(cfg.schedule)) > 0
    )
    rows = (await session.execute(stmt)).all()
    out: list[dict] = []
    for user_id, schedule in rows:
        last = (
            await session.execute(
                select(func.max(models.PipelineRun.created_at)).where(
                    models.PipelineRun.user_id == user_id,
                    models.PipelineRun.stage == UMBRELLA_STAGE,
                )
            )
        ).scalar_one_or_none()
        out.append({"user_id": str(user_id), "schedule": schedule, "last_run_at": _iso(last)})
    return out


# --------------------------------------------------------------------------- #
# Phase 6.5 — tailored CV drafts
# --------------------------------------------------------------------------- #
def cv_draft_to_dict(d: models.CvDraft) -> dict:
    return {
        "id": str(d.id),
        "job_id": str(d.job_id),
        "base_resume_version": str(d.base_resume_version) if d.base_resume_version else None,
        "tailored_content_json": d.tailored_content_json,
        "locked_snapshot_json": d.locked_snapshot_json,
        "critic_notes_json": d.critic_notes_json,
        "status": d.status,
        "before_score": d.before_score,
        "after_score": d.after_score,
        "pdf_ref": d.pdf_ref,
        "docx_ref": d.docx_ref,
        "created_at": _iso(d.created_at),
    }


async def upsert_cv_draft(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    base_resume_version: uuid.UUID | None,
    tailored_content_json: dict,
    locked_snapshot_json: dict,
    critic_notes_json: dict | None,
    before_score: float | None,
    status: str = "draft_ready",
) -> models.CvDraft:
    """One draft per (user, job); re-tailoring REPLACES the prior draft and resets it to
    the review stage (clears the previous export + after-score)."""
    existing = (
        await session.execute(
            select(models.CvDraft).where(
                models.CvDraft.user_id == user_id, models.CvDraft.job_id == job_id
            )
        )
    ).scalar_one_or_none()
    d = existing or models.CvDraft(user_id=user_id, job_id=job_id)
    d.base_resume_version = base_resume_version
    d.tailored_content_json = tailored_content_json
    d.locked_snapshot_json = locked_snapshot_json
    d.critic_notes_json = critic_notes_json
    d.before_score = before_score
    d.status = status
    d.after_score = None  # a fresh draft hasn't been re-qualified yet
    d.pdf_ref = None
    d.docx_ref = None
    if existing is None:
        session.add(d)
    await session.flush()
    return d


async def get_cv_drafts(
    session: AsyncSession, user_id: uuid.UUID, status: str | None = None
) -> list[dict]:
    stmt = select(models.CvDraft).where(models.CvDraft.user_id == user_id)
    if status:
        stmt = stmt.where(models.CvDraft.status == status)
    stmt = stmt.order_by(models.CvDraft.created_at.desc())
    return [cv_draft_to_dict(d) for d in (await session.execute(stmt)).scalars().all()]


async def get_cv_draft(
    session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID
) -> models.CvDraft | None:
    return (
        await session.execute(
            select(models.CvDraft).where(
                models.CvDraft.user_id == user_id, models.CvDraft.id == draft_id
            )
        )
    ).scalar_one_or_none()


async def set_cv_draft_status(
    session: AsyncSession, user_id: uuid.UUID, draft_ids: list[uuid.UUID], status: str
) -> int:
    """Bulk approve/reject (user-scoped). ``draft_ids`` empty means no-op. Returns count."""
    if not draft_ids:
        return 0
    rows = (
        await session.execute(
            select(models.CvDraft).where(
                models.CvDraft.user_id == user_id, models.CvDraft.id.in_(draft_ids)
            )
        )
    ).scalars().all()
    for d in rows:
        d.status = status
    if rows:
        await session.flush()
    return len(rows)


async def set_cv_draft_export(
    session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID,
    pdf_ref: str | None, docx_ref: str | None,
) -> models.CvDraft | None:
    d = await get_cv_draft(session, user_id, draft_id)
    if d is not None:
        d.pdf_ref = pdf_ref
        d.docx_ref = docx_ref
        await session.flush()
    return d


async def set_cv_draft_after_score(
    session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID, after_score: float
) -> models.CvDraft | None:
    d = await get_cv_draft(session, user_id, draft_id)
    if d is not None:
        d.after_score = after_score
        await session.flush()
    return d


async def set_cv_draft_content(
    session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID, tailored_content_json: dict
) -> models.CvDraft | None:
    """Overwrite a draft's tailored content (after an in-place chat edit) and RESET it to
    ``draft_ready`` so it must be re-approved before it re-attaches — a change to an
    already-approved/exported CV can never silently ship. Also DETACHES the stale rendered
    files (clears ``pdf_ref``/``docx_ref``) and the ``after_score``: the previously exported
    PDF/DOCX no longer matches the edited content, so until the user re-exports, the job
    falls back to the base résumé for scoring (``select_effective_resume`` needs ``pdf_ref``)
    and there's no downloadable file that misrepresents the draft. A re-export overwrites the
    same deterministic keys, so no orphan accumulates."""
    d = await get_cv_draft(session, user_id, draft_id)
    if d is not None:
        d.tailored_content_json = tailored_content_json
        d.status = "draft_ready"
        d.after_score = None
        d.pdf_ref = None
        d.docx_ref = None
        await session.flush()
    return d


# --------------------------------------------------------------------------- #
# Phase 7 — embeddings I/O (populate the pgvector columns; worker computes vectors)
# --------------------------------------------------------------------------- #
# kind -> (model, serializer). The embed text is built from the serialized row via
# jobpilot_core.embeddings.TEXT_BUILDERS, so this stays the single kind registry.
_EMBED_MODELS: dict[str, tuple] = {
    "jobs": (models.Job, "job_to_dict"),
    "analyses": (models.Analysis, "analysis_to_dict"),
    "suggestions": (models.Suggestion, "suggestion_to_dict"),
    "resumes": (models.Resume, "resume_to_dict"),
}


def _embed_row_dict(kind: str, row) -> dict:
    """Serialize a row for the embed-text builder. Resumes need raw_text (not in the
    default serializer), so add it here."""
    d = globals()[_EMBED_MODELS[kind][1]](row)
    if kind == "resumes":
        d["raw_text"] = row.raw_text
    return d


async def list_unembedded_texts(
    session: AsyncSession, user_id: uuid.UUID, kind: str, limit: int = 200
) -> list[dict]:
    """``[{id, text}]`` for the user's rows of ``kind`` that have no embedding yet, with
    a non-empty embed text. Rows whose text is empty are skipped (nothing to embed —
    they stay NULL and simply aren't semantically searchable)."""
    from .embeddings import TEXT_BUILDERS

    model, _ = _EMBED_MODELS[kind]
    build = TEXT_BUILDERS[kind]
    rows = (
        await session.execute(
            select(model)
            .where(model.user_id == user_id, model.embedding.is_(None))
            .order_by(model.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    out: list[dict] = []
    for row in rows:
        text = build(_embed_row_dict(kind, row))
        if text:
            out.append({"id": str(row.id), "text": text})
    return out


async def set_embeddings(
    session: AsyncSession, user_id: uuid.UUID, kind: str, items: list[dict]
) -> int:
    """Write ``[{id, embedding}]`` for the user's rows of ``kind`` (scoped, so a caller
    can only ever set embeddings on their own rows). Returns the number written."""
    model, _ = _EMBED_MODELS[kind]
    written = 0
    for item in items:
        row = (
            await session.execute(
                select(model).where(model.user_id == user_id, model.id == uuid.UUID(item["id"]))
            )
        ).scalar_one_or_none()
        if row is not None and item.get("embedding") is not None:
            row.embedding = item["embedding"]
            written += 1
    if written:
        await session.flush()
    return written


async def embedding_coverage(session: AsyncSession, user_id: uuid.UUID) -> dict[str, dict]:
    """Per-kind ``{total, embedded}`` — lets the backfill report progress + the status
    tool state how fresh the semantic index is."""
    out: dict[str, dict] = {}
    for kind, (model, _) in _EMBED_MODELS.items():
        total = (
            await session.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            )
        ).scalar_one()
        embedded = (
            await session.execute(
                select(func.count()).select_from(model).where(
                    model.user_id == user_id, model.embedding.is_not(None)
                )
            )
        ).scalar_one()
        out[kind] = {"total": int(total), "embedded": int(embedded)}
    return out


# --------------------------------------------------------------------------- #
# Phase 7 — semantic search (pgvector cosine similarity over embedded rows)
# --------------------------------------------------------------------------- #
async def semantic_search_jobs(
    session: AsyncSession, user_id: uuid.UUID, embedding: list[float], limit: int = 8
) -> list[dict]:
    """Nearest jobs to the query ``embedding`` by cosine similarity (embedded jobs
    only). Each result carries ``similarity`` (1 - cosine_distance) so the caller can
    ground + threshold. Joined with the qualification so answers can cite the decision."""
    dist = models.Job.embedding.cosine_distance(embedding)
    stmt = (
        select(models.Job, models.Qualification, dist.label("distance"))
        .outerjoin(
            models.Qualification,
            (models.Qualification.job_id == models.Job.id)
            & (models.Qualification.user_id == user_id),
        )
        .where(models.Job.user_id == user_id, models.Job.embedding.is_not(None))
        .order_by(dist)
        .limit(limit)
    )
    out: list[dict] = []
    for job, q, distance in (await session.execute(stmt)).all():
        d = job_to_dict(job)
        d["similarity"] = round(1.0 - float(distance), 4)
        d["qualification"] = (
            {"score": q.score, "decision": q.decision} if q is not None else None
        )
        out.append(d)
    return out


async def semantic_search_analyses(
    session: AsyncSession, user_id: uuid.UUID, embedding: list[float], limit: int = 8
) -> list[dict]:
    """Nearest job analyses to the query ``embedding`` (embedded analyses only)."""
    dist = models.Analysis.embedding.cosine_distance(embedding)
    stmt = (
        select(models.Analysis, dist.label("distance"))
        .where(models.Analysis.user_id == user_id, models.Analysis.embedding.is_not(None))
        .order_by(dist)
        .limit(limit)
    )
    out: list[dict] = []
    for analysis, distance in (await session.execute(stmt)).all():
        d = analysis_to_dict(analysis)
        d["similarity"] = round(1.0 - float(distance), 4)
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Phase 7 — exact structured queries (counts/aggregates computed IN SQL, never RAG)
# --------------------------------------------------------------------------- #
def _since(since_days: int | None):
    return datetime.now(timezone.utc) - timedelta(days=since_days) if since_days else None


async def count_jobs(
    session: AsyncSession,
    user_id: uuid.UUID,
    status: str | None = None,
    source: str | None = None,
    since_days: int | None = None,
) -> int:
    """Exact count of the user's jobs, optionally filtered by status / connector /
    recency. This is the answer to 'how many jobs …' — never a similarity search."""
    stmt = select(func.count()).select_from(models.Job).where(models.Job.user_id == user_id)
    if status:
        stmt = stmt.where(models.Job.status == status)
    if source:
        stmt = stmt.where(models.Job.connector == source)
    cutoff = _since(since_days)
    if cutoff is not None:
        stmt = stmt.where(models.Job.created_at >= cutoff)
    return int((await session.execute(stmt)).scalar_one())


async def count_qualifications(
    session: AsyncSession,
    user_id: uuid.UUID,
    decision: str | None = None,
    since_days: int | None = None,
) -> int:
    """Exact count of qualifications, optionally by decision (qualified|disqualified)
    and recency. 'How many qualified this week?' → this, not RAG."""
    stmt = (
        select(func.count())
        .select_from(models.Qualification)
        .where(models.Qualification.user_id == user_id)
    )
    if decision:
        stmt = stmt.where(models.Qualification.decision == decision)
    cutoff = _since(since_days)
    if cutoff is not None:
        stmt = stmt.where(models.Qualification.created_at >= cutoff)
    return int((await session.execute(stmt)).scalar_one())


async def _group_counts(session, model, column, user_id) -> list[dict]:
    rows = (
        await session.execute(
            select(column, func.count())
            .where(model.user_id == user_id)
            .group_by(column)
            .order_by(func.count().desc())
        )
    ).all()
    return [{"label": (label if label is not None else "unknown"), "count": int(n)} for label, n in rows]


async def job_stats(session: AsyncSession, user_id: uuid.UUID) -> dict:
    """A compact, exact breakdown of the user's pool: totals + counts by decision,
    source, and description completeness — all computed in SQL. The structured answer
    surface for 'give me a breakdown' questions."""
    total_jobs = await count_jobs(session, user_id)
    qualified = await count_qualifications(session, user_id, decision="qualified")
    disqualified = await count_qualifications(session, user_id, decision="disqualified")
    scored = await count_qualifications(session, user_id)
    return {
        "total_jobs": total_jobs,
        "scored": scored,
        "qualified": qualified,
        "disqualified": disqualified,
        "unscored": max(0, total_jobs - scored),
        "by_source": await _group_counts(session, models.Job, models.Job.connector, user_id),
        "by_completeness": await _group_counts(
            session, models.Job, models.Job.description_completeness, user_id),
    }


async def latest_stage_runs(session: AsyncSession, user_id: uuid.UUID) -> dict[str, dict]:
    """The most recent run of each stage (scout/qualify/analyze/enhance) with its
    status + finished_at + counts — the live 'when did X last run / is X running' truth,
    grounded in pipeline_runs (never in any narrative a job description contains)."""
    out: dict[str, dict] = {}
    for stage in ("scout", "qualify", "analyze", "enhance"):
        row = (
            await session.execute(
                select(models.PipelineRun)
                .where(models.PipelineRun.user_id == user_id, models.PipelineRun.stage == stage)
                .order_by(models.PipelineRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            out[stage] = pipeline_run_to_dict(row)
    return out


# --------------------------------------------------------------------------- #
# Erasure (GDPR) — hard delete + verification helpers
# --------------------------------------------------------------------------- #
async def cv_file_refs_for_jobs(
    session: AsyncSession, user_id: uuid.UUID, job_ids: list[uuid.UUID]
) -> list[str]:
    """The storage refs (rendered pdf/docx) of CV drafts attached to ``job_ids``. Collected
    BEFORE the jobs are deleted, because the ON DELETE CASCADE removes the ``cv_drafts``
    rows and would otherwise orphan their files in object storage. Scoped to ``user_id``."""
    if not job_ids:
        return []
    res = await session.execute(
        select(models.CvDraft.pdf_ref, models.CvDraft.docx_ref).where(
            models.CvDraft.user_id == user_id, models.CvDraft.job_id.in_(job_ids)
        )
    )
    refs: list[str] = []
    for pdf_ref, docx_ref in res.all():
        if pdf_ref:
            refs.append(pdf_ref)
        if docx_ref:
            refs.append(docx_ref)
    return refs


async def disqualified_job_ids(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Ids of the caller's jobs whose current qualification decision is ``disqualified``.
    Jobs with no qualification yet (unscored) are excluded — only an explicit disqualified
    verdict qualifies. Resolved up front so the caller can clean up files, then delete."""
    res = await session.execute(
        select(models.Job.id).where(
            models.Job.user_id == user_id,
            models.Job.id.in_(
                select(models.Qualification.job_id).where(
                    models.Qualification.user_id == user_id,
                    models.Qualification.decision == "disqualified",
                )
            ),
        )
    )
    return [row[0] for row in res.all()]


async def delete_jobs(
    session: AsyncSession, user_id: uuid.UUID, job_ids: list[uuid.UUID]
) -> int:
    """Delete specific jobs owned by the user. Their children — qualification, analysis,
    suggestions, and tailored CV drafts — cascade via each child's ON DELETE CASCADE FK
    to ``jobs``. Scoped to ``user_id`` so a caller can only ever delete their own rows.
    Rendered CV files are cleaned up separately (see ``cv_file_refs_for_jobs``), since the
    cascade removes the draft rows but not their storage objects. Returns rows deleted."""
    if not job_ids:
        return 0
    res = await session.execute(
        delete(models.Job).where(
            models.Job.user_id == user_id, models.Job.id.in_(job_ids)
        )
    )
    return int(res.rowcount or 0)


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
