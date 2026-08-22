"""resume-mcp — resume ingestion over HTTP (streamable MCP).

Extract text (PDF/DOCX) -> LLM structuring into the typed shape (a strong Claude model,
no rules-based parsing) -> store the file and persist the resume row (with consent +
retention). Resume text is treated as untrusted data by the structuring prompt; the
scoping ``user_id`` still comes only from the signed request context.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime, timedelta, timezone

from jobpilot_core import models, repositories
from jobpilot_core.db import session_scope
from jobpilot_core.mcp_auth import UserContextMiddleware, require_user_id
from jobpilot_core.resume_parse import parse_resume_bytes
from jobpilot_core.settings import get_settings
from jobpilot_core.storage import cv_key, get_storage, resume_key, user_prefix
from mcp.server.fastmcp import FastMCP

settings = get_settings()
mcp = FastMCP(
    "resume-mcp", stateless_http=True, json_response=True, host="0.0.0.0", port=8002
)


@mcp.tool()
async def parse_resume(
    file_b64: str,
    filename: str = "resume",
    declared_mime: str | None = None,
    consent: bool = False,
) -> dict:
    """Parse a resume into the typed shape; persist it (file + row) only with consent."""
    uid = require_user_id()
    data = base64.b64decode(file_b64)
    if len(data) > settings.resume_max_bytes:
        raise ValueError(f"file exceeds {settings.resume_max_bytes} bytes")

    raw_text, structured, mime = await parse_resume_bytes(data, filename, declared_mime, settings)

    if not consent:
        return {
            "resume_id": None,
            "stored": False,
            "mime": mime,
            "structured": structured.model_dump(),
        }

    resume_id = uuid.uuid4()
    key = resume_key(uid, resume_id, filename)
    storage = get_storage(settings)
    await asyncio.to_thread(storage.put_bytes, key, data, mime)

    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        session.add(
            models.Resume(
                id=resume_id,
                user_id=uid,
                file_ref=key,
                raw_text=raw_text,
                parsed_json=structured.model_dump(),
                consent_at=now,
                retention_expires_at=now + timedelta(days=settings.resume_retention_days),
            )
        )

    return {
        "resume_id": str(resume_id),
        "stored": True,
        "mime": mime,
        "file_ref": key,
        "structured": structured.model_dump(),
    }


@mcp.tool()
async def reparse_resume() -> dict:
    """Re-run extraction over the file already in storage and overwrite the parse in place.

    Not an upload. The file, the row and its id are all unchanged, so nothing pointing at
    this resume version is detached — a tailored CV's ``base_resume_version`` above all.
    This is how a parser improvement reaches a resume that predates it: the candidate
    should not have to find the file again, and should not lose work already done against
    it just to gain a field the old schema had no room for.

    Costs one model call. Returns ``{resume_id, reparsed, structured}`` or ``{error}``."""
    uid = require_user_id()
    async with session_scope() as session:
        resume = await repositories.get_latest_uploaded_resume(session, uid)
        if resume is None:
            return {"error": "no uploaded resume on file"}
        resume_id, ref = resume.id, resume.file_ref
    # Same scoping rule as get_resume_file: the ref must live under the caller's prefix.
    if not ref or not ref.startswith(user_prefix(uid)):
        return {"error": "no original file for this resume"}

    storage = get_storage(settings)
    data = await asyncio.to_thread(storage.get_bytes, ref)
    # declared_mime is None on purpose - the stored bytes are sniffed, so a lost or odd
    # extension on the stored key cannot misroute the extractor.
    raw_text, structured, _mime = await parse_resume_bytes(data, ref, None, settings)

    async with session_scope() as session:
        reparsed = await repositories.set_resume_parse(
            session, uid, resume_id, raw_text, structured.model_dump())
    return {
        "resume_id": str(resume_id),
        "reparsed": reparsed,
        "structured": structured.model_dump(),
    }


@mcp.tool()
async def get_structured_resume() -> dict:
    uid = require_user_id()
    async with session_scope() as session:
        resume = await repositories.get_latest_resume(session, uid)
        if resume is None:
            return {"resume": None}
        return {
            "resume": {
                "id": str(resume.id),
                "structured": resume.parsed_json,
                "source": resume.source,
                "label": resume.label,
                "consent_at": resume.consent_at.isoformat() if resume.consent_at else None,
                "created_at": resume.created_at.isoformat() if resume.created_at else None,
            }
        }


# --------------------------------------------------------------------------- #
# Resume versioning (Phase 6, 6e) — apply-to-resume writes a NEW version; the
# original is preserved and both remain retrievable/downloadable.
# --------------------------------------------------------------------------- #
@mcp.tool()
async def create_resume_version(
    structured: dict, raw_text: str, label: str | None = None, parent_id: str | None = None
) -> dict:
    """Persist an assembled resume version. Inherits consent/retention from the
    caller's latest resume (they already consented to processing); this is a new row,
    so the original is preserved. Resume content is never logged."""
    uid = require_user_id()
    async with session_scope() as session:
        latest = await repositories.get_latest_resume(session, uid)
        if latest is None:
            return {"error": "no resume on file to version"}
        resume = await repositories.create_resume_version(
            session, uid,
            parsed_json=structured,
            raw_text=raw_text,
            label=label,
            parent_id=uuid.UUID(parent_id) if parent_id else latest.id,
            consent_at=latest.consent_at,
            retention_expires_at=latest.retention_expires_at,
        )
        return {"resume": {"id": str(resume.id), "source": resume.source, "label": resume.label,
                           "parent_id": str(resume.parent_id) if resume.parent_id else None}}


@mcp.tool()
async def delete_assembled_versions(keep_id: str | None = None) -> dict:
    """Delete the caller's assembled apply-to-resume versions, optionally keeping one.

    Only ``source='assembled'`` rows are eligible — uploaded resumes can never be removed
    by this tool, so the working resume is safe regardless of what the caller passes.
    Assembled versions hold no stored file (synthetic ``file_ref``), so there is no
    storage object to clean up. Returns ``{deleted: n}``."""
    uid = require_user_id()
    async with session_scope() as session:
        deleted = await repositories.delete_assembled_resumes(
            session, uid, uuid.UUID(keep_id) if keep_id else None
        )
        return {"deleted": deleted}


@mcp.tool()
async def list_resume_versions() -> dict:
    """All of the caller's resume versions (upload + assembled), newest first."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"resumes": await repositories.list_resumes(session, uid)}


@mcp.tool()
async def get_base_resume() -> dict:
    """The caller's WORKING resume for scoring/tailoring = the latest *uploaded* resume
    (``source='upload'``). Assembled 'apply-to-resume' versions are downloadable artifacts,
    never the working resume — so applying suggestions can't take over qualification.
    Returns ``{resume: {id, structured} | None}``."""
    uid = require_user_id()
    async with session_scope() as session:
        resume = await repositories.get_latest_uploaded_resume(session, uid)
        if resume is None:
            return {"resume": None}
        return {"resume": {"id": str(resume.id), "structured": resume.parsed_json}}


@mcp.tool()
async def get_resume_file(resume_id: str | None = None) -> dict:
    """Fetch the ORIGINAL uploaded resume file (PDF/DOCX) as base64. Defaults to the
    caller's most recent *uploaded* resume; assembled versions have no stored file.
    Scoped: the ref MUST live under the caller's own storage prefix. Content is never
    logged. Returns ``{file_b64, file_ref}`` or ``{error}``."""
    uid = require_user_id()
    async with session_scope() as session:
        if resume_id:
            resume = await repositories.get_resume(session, uid, uuid.UUID(resume_id))
        else:
            resume = await repositories.get_latest_uploaded_resume(session, uid)
        if resume is None:
            return {"error": "no uploaded resume on file"}
        ref = resume.file_ref
    if not ref or not ref.startswith(user_prefix(uid)):
        return {"error": "no original file for this resume"}  # e.g. an assembled:// version
    storage = get_storage(settings)
    data = await asyncio.to_thread(storage.get_bytes, ref)
    return {"file_b64": base64.b64encode(data).decode("ascii"), "file_ref": ref}


@mcp.tool()
async def get_resume_version(resume_id: str) -> dict:
    """One resume version, including its downloadable ``raw_text`` rendering."""
    uid = require_user_id()
    async with session_scope() as session:
        resume = await repositories.get_resume(session, uid, uuid.UUID(resume_id))
        if resume is None:
            return {"resume": None}
        d = repositories.resume_to_dict(resume)
        d["raw_text"] = resume.raw_text
        return {"resume": d}


# --------------------------------------------------------------------------- #
# Tailored CV files (Phase 6.5) — rendered PDF/DOCX bytes, stored UNDER the user's
# storage prefix so delete_user_data (which deletes by that prefix) wipes them too.
# resume-mcp owns storage, so CV file I/O lives here. Content is never logged.
# --------------------------------------------------------------------------- #
@mcp.tool()
async def put_cv_file(draft_id: str, fmt: str, file_b64: str) -> dict:
    """Store a rendered tailored-CV file (fmt = pdf|docx). Returns the storage ref."""
    uid = require_user_id()
    if fmt not in ("pdf", "docx"):
        return {"error": f"bad fmt {fmt!r} (expected pdf|docx)"}
    data = base64.b64decode(file_b64)
    key = cv_key(uid, draft_id, fmt)
    ctype = "application/pdf" if fmt == "pdf" else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    storage = get_storage(settings)
    await asyncio.to_thread(storage.put_bytes, key, data, ctype)
    return {"ref": key, "fmt": fmt, "bytes": len(data)}


@mcp.tool()
async def get_cv_file(ref: str) -> dict:
    """Fetch a rendered tailored-CV file as base64. Scoped: the ref MUST live under the
    caller's own storage prefix (a caller can never read another user's file)."""
    uid = require_user_id()
    if not ref or not ref.startswith(user_prefix(uid)):
        return {"error": "not found"}
    storage = get_storage(settings)
    data = await asyncio.to_thread(storage.get_bytes, ref)
    return {"file_b64": base64.b64encode(data).decode("ascii"), "bytes": len(data)}


app = UserContextMiddleware(mcp.streamable_http_app(), settings)
