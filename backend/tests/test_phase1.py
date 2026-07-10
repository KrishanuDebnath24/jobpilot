"""Phase 1 integration tests over the real MCP HTTP boundary.

Covers the acceptance gate: cross-user isolation incl. spoofed user_id, unauth
rejection, job round-trip, PDF+DOCX resume ingestion, and erasure of rows + files.
"""

from __future__ import annotations

import io
import json
import uuid

import docx
from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.security import hash_password, mint_mcp_context_token
from jobpilot_core.settings import get_settings
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

S = get_settings()
RESUME_LINES = [
    "Jane Doe",
    "jane.doe@example.com | +353 87 123 4567",
    "Skills",
    "Python, FastAPI, PostgreSQL, Docker",
    "Experience",
    "Senior Engineer at Acme",
    "- Built data pipelines",
    "Education",
    "BSc Computer Science, Trinity College Dublin",
    "Certifications",
    "AWS Certified Solutions Architect - Associate",
    "Certified Kubernetes Administrator",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _mk_user() -> uuid.UUID:
    email = f"t_{uuid.uuid4().hex}@example.com"
    async with session_scope() as s:
        user = await repositories.create_user(s, email, hash_password("x"))
        return user.id


def _tok(uid: uuid.UUID) -> str:
    return mint_mcp_context_token(uid, S)


def _data(res):
    sc = res.structuredContent
    if sc is not None:
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    return json.loads(res.content[0].text) if res.content else None


async def _call(url: str, token: str | None, tool: str, args: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return await session.call_tool(tool, args or {})


def _pdf() -> str:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    for ln in RESUME_LINES:
        c.drawString(72, y, ln)
        y -= 22
    c.save()
    import base64

    return base64.b64encode(buf.getvalue()).decode("ascii")


def _docx() -> str:
    d = docx.Document()
    for ln in RESUME_LINES:
        d.add_paragraph(ln)
    buf = io.BytesIO()
    d.save(buf)
    import base64

    return base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_unauthenticated_call_is_refused(mcp_services):
    res = await _call(mcp_services["db"], None, "get_jobs")
    assert res.isError is True


async def test_isolation_spoofed_user_id_and_round_trip(mcp_services):
    a, b = await _mk_user(), await _mk_user()
    try:
        await _call(mcp_services["db"], _tok(a), "upsert_jobs", {"jobs": [
            {"title": "A One", "source_type": "seed", "dedupe_hash": f"{a}-1"},
            {"title": "A Two", "source_type": "seed", "dedupe_hash": f"{a}-2"},
        ]})
        await _call(mcp_services["db"], _tok(b), "upsert_jobs", {"jobs": [
            {"title": "B One", "source_type": "seed", "dedupe_hash": f"{b}-1"},
        ]})

        ja = _data(await _call(mcp_services["db"], _tok(a), "get_jobs"))["jobs"]
        jb = _data(await _call(mcp_services["db"], _tok(b), "get_jobs"))["jobs"]
        assert len(ja) == 2 and len(jb) == 1  # round-trip
        assert all({"id", "title", "status"} <= set(j) for j in ja)  # normalized

        # A authenticated as A but explicitly passes B's user_id -> must be ignored
        spoof = _data(await _call(mcp_services["db"], _tok(a), "get_jobs", {"user_id": str(b)}))["jobs"]
        assert {j["title"] for j in spoof} == {"A One", "A Two"}
        assert "B One" not in {j["title"] for j in spoof}
    finally:
        await _call(mcp_services["db"], _tok(a), "delete_user_data")
        await _call(mcp_services["db"], _tok(b), "delete_user_data")


async def test_resume_pdf_and_docx(mcp_services):
    uid = await _mk_user()
    try:
        pdf = _data(await _call(mcp_services["resume"], _tok(uid), "parse_resume",
                                {"file_b64": _pdf(), "filename": "r.pdf",
                                 "declared_mime": "application/pdf", "consent": True}))
        assert pdf["stored"] is True
        assert "Python" in pdf["structured"]["skills"]
        assert pdf["structured"]["contact"]["email"] == "jane.doe@example.com"
        assert "Certified Kubernetes Administrator" in pdf["structured"]["certifications"]

        DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        dx = _data(await _call(mcp_services["resume"], _tok(uid), "parse_resume",
                               {"file_b64": _docx(), "filename": "r.docx",
                                "declared_mime": DOCX, "consent": True}))
        assert dx["stored"] is True
        assert "Docker" in dx["structured"]["skills"]

        latest = _data(await _call(mcp_services["resume"], _tok(uid), "get_structured_resume"))
        assert latest["resume"] is not None
    finally:
        await _call(mcp_services["db"], _tok(uid), "delete_user_data")


async def test_erasure_wipes_rows_and_files(mcp_services):
    uid = await _mk_user()
    await _call(mcp_services["db"], _tok(uid), "upsert_jobs",
                {"jobs": [{"title": "keepme", "source_type": "seed", "dedupe_hash": f"{uid}-k"}]})
    await _call(mcp_services["resume"], _tok(uid), "parse_resume",
                {"file_b64": _pdf(), "filename": "r.pdf",
                 "declared_mime": "application/pdf", "consent": True})

    user_dir = mcp_services["storage_dir"] / "resumes" / str(uid)
    assert user_dir.exists()  # file was stored

    result = _data(await _call(mcp_services["db"], _tok(uid), "delete_user_data"))
    assert result["deleted"] is True
    assert result["files_deleted"] >= 1
    assert all(v == 0 for v in result["remaining_counts"].values())
    assert not user_dir.exists()  # stored files gone
