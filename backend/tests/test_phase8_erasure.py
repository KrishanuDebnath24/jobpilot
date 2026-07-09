"""Phase 8f — GDPR / erasure completeness on a FULLY-populated user (DB-gated).

Erasure was verified per-piece across phases; 8f proves ``delete_user_data``'s logic
(delete storage by the user prefix + cascade every user-owned table + verify zero remain)
leaves **zero residue** across the whole grown surface at once: resume + an assembled
version, jobs + embeddings, qualifications, analyses, suggestions, cv_drafts + their
rendered PDF/DOCX files, chat session + messages, pipeline_runs, and a config — files and
vectors included.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from jobpilot_core import embeddings, repositories as R
from jobpilot_core.security import hash_password
from jobpilot_core.settings import Settings
from jobpilot_core.storage import LocalStorageClient, cv_key, resume_key, user_prefix

from .conftest import _db_reachable

requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")
HS = Settings(embedding_backend="hashing")


async def _seed_job(s, uid):
    from jobpilot_core.schemas import JobIn
    from jobpilot_core.scout import normalize_raw

    jobs = await R.upsert_jobs(s, uid, [JobIn.model_validate(normalize_raw({
        "connector": "greenhouse:acme", "source": "greenhouse:acme", "source_type": "ats",
        "company": "Acme", "title": "Backend Engineer", "location": "Remote",
        "url": f"https://x/{uuid.uuid4().hex}", "description": "Go, Kubernetes, Postgres.",
        "description_completeness": "full"}).model_dump())])
    return jobs[0].id


@requires_db
async def test_erasure_leaves_zero_residue_on_a_fully_populated_user(tmp_path):
    from jobpilot_core.db import session_scope

    storage = LocalStorageClient(str(tmp_path))
    now = dt.datetime.now(dt.timezone.utc)

    async with session_scope() as s:
        uid = (await R.create_user(s, f"p8f_{uuid.uuid4().hex}@ex.com", hash_password("x"))).id

        # resume + its uploaded FILE (under the user prefix), plus an assembled version
        rk = resume_key(uid, uuid.uuid4(), "cv.pdf")
        storage.put_bytes(rk, b"%PDF-1.4 resume", "application/pdf")
        await R.create_resume(s, uid, rk, "raw resume text", {"skills": ["Go"]}, now, now)
        await R.create_resume_version(s, uid, {"skills": ["Go"]}, "assembled text", "v2", None, now, now)

        # job + embedding, qualification, analysis, suggestion
        jid = await _seed_job(s, uid)
        items = await R.list_unembedded_texts(s, uid, "jobs", limit=10)
        vecs = embeddings.embed_texts(HS, [it["text"] for it in items])
        await R.set_embeddings(s, uid, "jobs", [{"id": it["id"], "embedding": v} for it, v in zip(items, vecs)])
        await R.upsert_qualification(s, uid, jid, 0.8, "qualified", None, [], [])
        await R.upsert_analysis(s, uid, jid, {"required_skills": ["Go"]}, None, "senior")
        await R.upsert_suggestion(s, uid, jid, ["Go"], None, None)

        # cv_draft + its rendered PDF/DOCX FILES (under the user prefix)
        d = await R.upsert_cv_draft(s, uid, jid, None, {"skills": ["Go"]}, {}, None, 0.5)
        pdf_k, docx_k = cv_key(uid, d.id, "pdf"), cv_key(uid, d.id, "docx")
        storage.put_bytes(pdf_k, b"%PDF-1.4 tailored", "application/pdf")
        storage.put_bytes(docx_k, b"PK docx tailored", "application/vnd.openxmlformats")
        await R.set_cv_draft_export(s, uid, d.id, pdf_k, docx_k)

        # chat session + message, a pipeline run, a search config
        cs = await R.create_chat_session(s, uid, "chat")
        await R.append_message(s, uid, cs.id, "user", "a private message")
        run = await R.create_pipeline_run(s, uid, "scout")
        await R.finish_pipeline_run(s, uid, run.id, "success", output_ref='{"new": 1}')
        await R.upsert_search_config(s, uid, {})

    # pre-erasure: the three files exist, and the residue-risk tables are populated
    assert storage.get_bytes(rk) and storage.get_bytes(pdf_k) and storage.get_bytes(docx_k)
    async with session_scope() as s:
        pre = await R.count_user_rows(s, uid)
    for t in ("resumes", "jobs", "qualifications", "analyses", "suggestions", "cv_drafts",
              "chat_sessions", "messages", "pipeline_runs", "search_configs"):
        assert pre[t] >= 1, (t, pre)
    assert pre["resumes"] == 2  # upload + assembled version

    # erase exactly as delete_user_data does: storage prefix, then cascade, then verify
    files_deleted = storage.delete_prefix(user_prefix(uid))
    async with session_scope() as s:
        await R.delete_user_cascade(s, uid)
        await s.flush()
        remaining = await R.count_user_rows(s, uid)

    # ZERO residue: every user-owned table + users == 0, all 3 files gone (incl. embeddings,
    # which are columns on the now-deleted rows)
    assert all(v == 0 for v in remaining.values()), remaining
    assert files_deleted == 3, files_deleted                 # resume + cv pdf + cv docx
    assert storage.delete_prefix(user_prefix(uid)) == 0      # nothing left under the prefix
    with pytest.raises(FileNotFoundError):
        storage.get_bytes(rk)                                # the file is truly gone
