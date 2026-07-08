"""resume-mcp — deterministic resume ingestion over HTTP (streamable MCP).

Extract text (PDF/DOCX) -> rules-based structured shape -> store the file and
persist the resume row (with consent + retention). NO LLM calls this phase. The
scoping ``user_id`` comes only from the signed request context.
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

    raw_text, structured, mime = parse_resume_bytes(data, filename, declared_mime)

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
async def list_resume_versions() -> dict:
    """All of the caller's resume versions (upload + assembled), newest first."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"resumes": await repositories.list_resumes(session, uid)}


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
