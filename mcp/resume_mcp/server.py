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
from jobpilot_core.storage import get_storage, resume_key
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
                "consent_at": resume.consent_at.isoformat() if resume.consent_at else None,
                "created_at": resume.created_at.isoformat() if resume.created_at else None,
            }
        }


app = UserContextMiddleware(mcp.streamable_http_app(), settings)
