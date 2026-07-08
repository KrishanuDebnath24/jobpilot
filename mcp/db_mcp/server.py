"""db-mcp — scoped CRUD over HTTP (streamable MCP).

Security core: the caller's ``user_id`` is derived ONLY from the signed request
context (validated by :class:`UserContextMiddleware`). Any ``user_id`` a caller
passes as a tool argument is accepted and then **ignored** — see ``get_jobs`` /
``delete_user_data``. Unauthenticated requests raise and return an error.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.embeddings import EMBED_KINDS
from jobpilot_core.mcp_auth import UserContextMiddleware, require_user_id
from jobpilot_core.schemas import JobIn
from jobpilot_core.settings import get_settings
from jobpilot_core.storage import get_storage, user_prefix
from mcp.server.fastmcp import FastMCP

settings = get_settings()
mcp = FastMCP("db-mcp", stateless_http=True, json_response=True, host="0.0.0.0", port=8001)


@mcp.tool()
async def get_jobs(
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user_id: str | None = None,  # IGNORED — scoping comes from the request context
) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        jobs = await repositories.get_jobs(session, uid, limit=limit, offset=offset, status=status)
        return {"jobs": [repositories.job_to_dict(j) for j in jobs]}


@mcp.tool()
async def get_job(job_id: str) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        job = await repositories.get_job(session, uid, uuid.UUID(job_id))
        return {"job": repositories.job_to_dict(job) if job else None}


@mcp.tool()
async def upsert_jobs(jobs: list[dict]) -> dict:
    uid = require_user_id()
    parsed = [JobIn.model_validate(j) for j in jobs]
    async with session_scope() as session:
        saved = await repositories.upsert_jobs(session, uid, parsed)
        return {"jobs": [repositories.job_to_dict(j) for j in saved]}


@mcp.tool()
async def search_jobs(query: str, limit: int = 20) -> dict:
    """Phase-1 keyword fallback (semantic embedding search lands in Phase 3)."""
    uid = require_user_id()
    async with session_scope() as session:
        jobs = await repositories.search_jobs(session, uid, query, limit=limit)
        return {"jobs": [repositories.job_to_dict(j) for j in jobs], "mode": "keyword"}


@mcp.tool()
async def get_resume() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        resume = await repositories.get_latest_resume(session, uid)
        return {"resume": repositories.resume_to_dict(resume) if resume else None}


@mcp.tool()
async def get_qualifications() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"qualifications": await repositories.get_qualifications(session, uid)}


@mcp.tool()
async def upsert_qualification(
    job_id: str,
    score: float | None = None,
    decision: str | None = None,
    reasons_json: dict | None = None,
    matched_skills: list[str] | None = None,
    missing_skills: list[str] | None = None,
) -> dict:
    """Persist a qualification (one per user+job; re-qualify overwrites)."""
    uid = require_user_id()
    async with session_scope() as session:
        q = await repositories.upsert_qualification(
            session, uid, uuid.UUID(job_id), score, decision, reasons_json,
            matched_skills or [], missing_skills or [],
        )
        return {"qualification": {"id": str(q.id), "job_id": str(q.job_id), "decision": q.decision}}


@mcp.tool()
async def get_qualified_jobs(limit: int = 1000) -> dict:
    """Jobs left-joined with their qualification (for the dashboard)."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"jobs": await repositories.get_qualified_jobs(session, uid, limit=limit)}


@mcp.tool()
async def get_analyses() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"analyses": await repositories.get_analyses(session, uid)}


@mcp.tool()
async def get_suggestions() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"suggestions": await repositories.get_suggestions(session, uid)}


@mcp.tool()
async def upsert_analysis(
    job_id: str,
    extracted_json: dict,
    gaps_json: dict | None = None,
    seniority: str | None = None,
    batch_id: str | None = None,
) -> dict:
    """Persist a job analysis (Phase 5, 5a). One per user+job; re-analyze overwrites."""
    uid = require_user_id()
    async with session_scope() as session:
        a = await repositories.upsert_analysis(
            session, uid, uuid.UUID(job_id), extracted_json, gaps_json, seniority,
            batch_id=uuid.UUID(batch_id) if batch_id else None,
        )
        return {"analysis": {"id": str(a.id), "job_id": str(a.job_id)}}


@mcp.tool()
async def upsert_suggestion(
    job_id: str | None = None,
    skills_to_add: list[str] | None = None,
    projects_json: dict | None = None,
    bullet_rewrites_json: dict | None = None,
    batch_id: str | None = None,
) -> dict:
    """Persist a suggestion (Phase 5, 5d). ``job_id=None`` = the batch aggregate."""
    uid = require_user_id()
    async with session_scope() as session:
        s = await repositories.upsert_suggestion(
            session, uid, uuid.UUID(job_id) if job_id else None,
            skills_to_add or [], projects_json, bullet_rewrites_json,
            batch_id=uuid.UUID(batch_id) if batch_id else None,
        )
        return {"suggestion": {"id": str(s.id), "job_id": str(s.job_id) if s.job_id else None,
                               "status": s.status}}


@mcp.tool()
async def update_suggestion_status(suggestion_id: str, status: str) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        s = await repositories.update_suggestion_status(session, uid, uuid.UUID(suggestion_id), status)
        return {"suggestion": {"id": str(s.id), "status": s.status} if s else None}


@mcp.tool()
async def create_pipeline_run(stage: str, pipeline_id: str | None = None) -> dict:
    """Open a stage run. ``pipeline_id`` correlates it with the umbrella execution
    it belongs to (Phase 4); None for a legacy/standalone stage run."""
    uid = require_user_id()
    pid = uuid.UUID(pipeline_id) if pipeline_id else None
    async with session_scope() as session:
        run = await repositories.create_pipeline_run(session, uid, stage, pipeline_id=pid)
        return {"id": str(run.id), "stage": run.stage, "status": run.status,
                "pipeline_id": pipeline_id}


@mcp.tool()
async def open_gate_pending(pipeline_id: str, gate_key: str) -> dict:
    """Mark a review gate pending for a pipeline (Phase 6). Idempotent — a resumed
    pipeline that re-enters the gate keeps the existing row. Read the gate's status
    back via ``get_stage_status`` with stage ``gate:<gate_key>``."""
    uid = require_user_id()
    async with session_scope() as session:
        row = await repositories.open_gate_pending(session, uid, uuid.UUID(pipeline_id), gate_key)
        return {"id": str(row.id), "stage": row.stage, "status": row.status,
                "pipeline_id": pipeline_id}


@mcp.tool()
async def get_stage_status(pipeline_id: str, stage: str) -> dict:
    """The persisted status of one stage of one pipeline — the *source of truth* the
    orchestrator reconciles against so a dropped pub/sub event can't hang a run."""
    uid = require_user_id()
    async with session_scope() as session:
        row = await repositories.get_stage_status(session, uid, uuid.UUID(pipeline_id), stage)
        return {"run": row}


@mcp.tool()
async def finish_pipeline_run(
    run_id: str, status: str, counts: dict | None = None, error: str | None = None
) -> dict:
    """Close out a run: set status/finished_at, persist the counts JSON, and an
    optional short error summary."""
    uid = require_user_id()
    output_ref = json.dumps(counts) if counts is not None else None
    async with session_scope() as session:
        run = await repositories.finish_pipeline_run(
            session, uid, uuid.UUID(run_id), status, output_ref=output_ref, error=error
        )
        return {"run": repositories.pipeline_run_to_dict(run) if run else None}


@mcp.tool()
async def get_pipeline_runs(stage: str | None = None, limit: int = 20) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"runs": await repositories.get_pipeline_runs(session, uid, stage=stage, limit=limit)}


# --------------------------------------------------------------------------- #
# Chat sessions + messages (Phase 6 Concierge) — persisted conversation
# --------------------------------------------------------------------------- #
@mcp.tool()
async def create_chat_session(title: str | None = None) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        cs = await repositories.create_chat_session(session, uid, title)
        return {"id": str(cs.id), "title": cs.title}


@mcp.tool()
async def list_chat_sessions(limit: int = 50) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"sessions": await repositories.list_chat_sessions(session, uid, limit=limit)}


@mcp.tool()
async def append_message(
    chat_session_id: str, role: str, content: str | None = None, tool_calls_json: dict | None = None
) -> dict:
    """Append one message to a chat session the caller owns. Returns None if the
    session doesn't belong to the caller (scoping is enforced, never spoofable)."""
    uid = require_user_id()
    async with session_scope() as session:
        m = await repositories.append_message(
            session, uid, uuid.UUID(chat_session_id), role, content, tool_calls_json)
        return {"message": repositories.message_to_dict(m) if m else None}


@mcp.tool()
async def get_messages(chat_session_id: str, limit: int = 200) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"messages": await repositories.get_messages(
            session, uid, uuid.UUID(chat_session_id), limit=limit)}


# --------------------------------------------------------------------------- #
# Phase 6.5 — tailored CV drafts (locked-safe; content never logged)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def create_cv_draft(
    job_id: str,
    tailored_content_json: dict,
    locked_snapshot_json: dict,
    base_resume_version: str | None = None,
    critic_notes_json: dict | None = None,
    before_score: float | None = None,
    status: str = "draft_ready",
) -> dict:
    """Upsert a tailored CV draft for (caller, job). Re-tailoring replaces the prior one."""
    uid = require_user_id()
    async with session_scope() as session:
        d = await repositories.upsert_cv_draft(
            session, uid, uuid.UUID(job_id),
            uuid.UUID(base_resume_version) if base_resume_version else None,
            tailored_content_json, locked_snapshot_json, critic_notes_json,
            before_score, status=status,
        )
        return {"draft": {"id": str(d.id), "job_id": str(d.job_id), "status": d.status}}


@mcp.tool()
async def get_cv_drafts(status: str | None = None) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        return {"drafts": await repositories.get_cv_drafts(session, uid, status=status)}


@mcp.tool()
async def get_cv_draft(draft_id: str) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        d = await repositories.get_cv_draft(session, uid, uuid.UUID(draft_id))
        return {"draft": repositories.cv_draft_to_dict(d) if d else None}


@mcp.tool()
async def set_cv_draft_status(draft_ids: list[str], status: str) -> dict:
    """Bulk approve/reject drafts the caller owns. status ∈ draft_ready|approved|rejected."""
    uid = require_user_id()
    if status not in ("draft_ready", "approved", "rejected"):
        return {"error": f"bad status {status!r}"}
    async with session_scope() as session:
        n = await repositories.set_cv_draft_status(
            session, uid, [uuid.UUID(x) for x in draft_ids], status)
        return {"updated": n, "status": status}


@mcp.tool()
async def set_cv_draft_export(draft_id: str, pdf_ref: str | None, docx_ref: str | None) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        d = await repositories.set_cv_draft_export(
            session, uid, uuid.UUID(draft_id), pdf_ref, docx_ref)
        return {"draft": {"id": str(d.id), "pdf_ref": d.pdf_ref, "docx_ref": d.docx_ref} if d else None}


@mcp.tool()
async def set_cv_draft_after_score(draft_id: str, after_score: float) -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        d = await repositories.set_cv_draft_after_score(session, uid, uuid.UUID(draft_id), after_score)
        return {"draft": {"id": str(d.id), "after_score": d.after_score} if d else None}


# --------------------------------------------------------------------------- #
# Phase 7 — embeddings I/O (the worker computes vectors; db-mcp stores/searches them)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_unembedded(kind: str, limit: int = 200) -> dict:
    """Rows of ``kind`` (jobs|analyses|suggestions|resumes) with no embedding yet, as
    ``[{id, text}]`` — the worker embeds ``text`` and writes back via ``set_embeddings``."""
    uid = require_user_id()
    if kind not in EMBED_KINDS:
        return {"error": f"unknown kind {kind!r} (expected {list(EMBED_KINDS)})"}
    async with session_scope() as session:
        return {"items": await repositories.list_unembedded_texts(session, uid, kind, limit=limit)}


@mcp.tool()
async def set_embeddings(kind: str, items: list[dict]) -> dict:
    """Persist embeddings ``[{id, embedding}]`` for the caller's rows of ``kind``."""
    uid = require_user_id()
    if kind not in EMBED_KINDS:
        return {"error": f"unknown kind {kind!r} (expected {list(EMBED_KINDS)})"}
    async with session_scope() as session:
        return {"written": await repositories.set_embeddings(session, uid, kind, items)}


@mcp.tool()
async def embedding_coverage() -> dict:
    """Per-kind {total, embedded} — how much of the pool is semantically searchable."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"coverage": await repositories.embedding_coverage(session, uid)}


@mcp.tool()
async def semantic_search_jobs(embedding: list[float], limit: int = 8) -> dict:
    """Nearest jobs to a query ``embedding`` by cosine similarity (semantic — for FUZZY
    questions only; counts/aggregates use the exact tools). Each carries ``similarity``."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"jobs": await repositories.semantic_search_jobs(session, uid, embedding, limit=limit)}


@mcp.tool()
async def semantic_search_analyses(embedding: list[float], limit: int = 8) -> dict:
    """Nearest job analyses to a query ``embedding`` by cosine similarity."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"analyses": await repositories.semantic_search_analyses(session, uid, embedding, limit=limit)}


# --------------------------------------------------------------------------- #
# Phase 7 — exact structured queries (counts/aggregates in SQL; the anti-RAG path)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def count_jobs(
    status: str | None = None, source: str | None = None, since_days: int | None = None
) -> dict:
    """EXACT count of the user's jobs (optional status/connector/recency filter)."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"count": await repositories.count_jobs(
            session, uid, status=status, source=source, since_days=since_days)}


@mcp.tool()
async def count_qualifications(decision: str | None = None, since_days: int | None = None) -> dict:
    """EXACT count of qualifications (optional decision=qualified|disqualified + recency)."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"count": await repositories.count_qualifications(
            session, uid, decision=decision, since_days=since_days)}


@mcp.tool()
async def job_stats() -> dict:
    """EXACT pool breakdown: totals + counts by decision, source, and completeness."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"stats": await repositories.job_stats(session, uid)}


@mcp.tool()
async def latest_stage_runs() -> dict:
    """The most recent scout/qualify/analyze/enhance run (status + finished_at + counts)
    — the live 'when did X last run' truth, grounded in pipeline_runs."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"stages": await repositories.latest_stage_runs(session, uid)}


@mcp.tool()
async def delete_user_data(user_id: str | None = None) -> dict:
    """GDPR erasure: hard-delete every row keyed to the caller (cascade) AND their
    stored resume files. ``user_id`` argument is IGNORED; scope comes from context.
    """
    uid = require_user_id()
    storage = get_storage(settings)
    # 1) delete storage objects for this user (best-effort, outside the DB tx)
    files_deleted = await asyncio.to_thread(storage.delete_prefix, user_prefix(uid))
    # 2) delete DB rows (users -> cascade), then verify zero remain in-transaction
    async with session_scope() as session:
        await repositories.delete_user_cascade(session, uid)
        await session.flush()
        remaining = await repositories.count_user_rows(session, uid)
    return {"deleted": True, "files_deleted": files_deleted, "remaining_counts": remaining}


# ASGI app: validate the signed context on every request, bind user_id for the call.
app = UserContextMiddleware(mcp.streamable_http_app(), settings)
