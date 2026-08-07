"""Phase 8d — end-to-end security review (the aggregate, not per-component).

Three deterministic guarantees over the whole grown surface:

  * **Cross-user isolation matrix** (DB-gated): user B can never read user A's rows through
    ANY read path — jobs, qualifications, analyses, suggestions, cv_drafts, chat messages,
    pipeline_runs, and *embeddings* (a semantic search on A's own text still returns only
    B's rows). Erasing A leaves B intact.
  * **user_id-from-session invariant**: both agent executors (Concierge, Query) bind
    user_id from the session and STRIP any user_id a tool tries to pass.
  * **Secrets audit**: no live Anthropic key in tracked files, the real .env isn't tracked,
    and the frontend references no server secret (only NEXT_PUBLIC_*).

The live full-chain injection case (a poisoned job through Analyze→Enhance→Query causing
no action/exfiltration/fabrication) is in ``test_phase8_live.py`` (key-gated).
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from jobpilot_core import embeddings, repositories as R
from jobpilot_core.security import hash_password
from jobpilot_core.settings import Settings

from .conftest import _db_reachable

REPO_ROOT = Path(__file__).resolve().parents[2]
HS = Settings(embedding_backend="hashing")
requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


# --------------------------------------------------------------------------- #
# user_id-from-session invariant (deterministic, no DB) — both executors
# --------------------------------------------------------------------------- #
async def test_both_executors_bind_user_id_from_session_not_args():
    from app.concierge_runner import ConciergeExecutor
    from app.query_runner import QueryExecutor

    session_uid = uuid.uuid4()
    spoof = str(uuid.uuid4())
    captured: list[tuple] = []

    async def fake_call(user_id, service, tool, args):
        captured.append((user_id, tool, dict(args)))
        return {"jobs": [], "drafts": [], "count": 0}

    # Query agent read with a spoofed user_id in args
    await QueryExecutor(session_uid, call=fake_call).execute("count_jobs", {"user_id": spoof})
    # Concierge read with a spoofed user_id in args
    await ConciergeExecutor(session_uid, call=fake_call).execute("get_dashboard", {"user_id": spoof})

    assert captured, "expected the executors to dispatch"
    for used_uid, _tool, args in captured:
        assert used_uid == session_uid          # bound from the session
        assert "user_id" not in args             # the spoofed arg was stripped
        assert str(used_uid) != spoof


# --------------------------------------------------------------------------- #
# Secrets audit (deterministic, no DB) — scans TRACKED files via git
# --------------------------------------------------------------------------- #
def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True)


def test_no_live_anthropic_key_in_tracked_files():
    # A real key is `sk-ant-` + a long token; the bare `sk-ant-` prefix used in
    # code/tests/docs (startswith checks) must NOT match. git grep exits 1 on no match.
    r = _git("grep", "-nE", r"sk-ant-[A-Za-z0-9_-]{20,}")
    assert r.returncode == 1, f"possible leaked Anthropic key in a tracked file:\n{r.stdout}"


def test_real_dotenv_is_not_tracked():
    r = _git("ls-files", ".env")
    assert r.stdout.strip() == "", "the real .env must never be committed"


def test_frontend_references_no_server_secrets():
    # The frontend container only ever gets NEXT_PUBLIC_*; no server secret may appear.
    r = _git("grep", "-nE",
             r"ANTHROPIC_API_KEY|JWT_SECRET|MCP_SHARED_SECRET|DATABASE_URL|APIFY_API_TOKEN|S3_SECRET_KEY",
             "--", "frontend/")
    assert r.returncode == 1, f"server secret referenced in the frontend:\n{r.stdout}"


# --------------------------------------------------------------------------- #
# Cross-user isolation matrix (DB-gated)
# --------------------------------------------------------------------------- #
async def _mk_user(s, tag):
    return (await R.create_user(s, f"p8d_{tag}_{uuid.uuid4().hex}@ex.com", hash_password("x"))).id


async def _seed_job(s, uid, title, desc):
    from jobpilot_core.schemas import JobIn
    from jobpilot_core.scout import normalize_raw

    jobs = await R.upsert_jobs(s, uid, [JobIn.model_validate(normalize_raw({
        "connector": "greenhouse:acme", "source": "greenhouse:acme", "source_type": "ats",
        "company": "Acme", "title": title, "location": "Remote",
        "url": f"https://x/{uuid.uuid4().hex}", "description": desc,
        "description_completeness": "full"}).model_dump())])
    return jobs[0].id


async def _embed_jobs(s, uid):
    items = await R.list_unembedded_texts(s, uid, "jobs", limit=50)
    vecs = embeddings.embed_texts(HS, [it["text"] for it in items])
    await R.set_embeddings(s, uid, "jobs", [{"id": it["id"], "embedding": v} for it, v in zip(items, vecs)])


@requires_db
async def test_cross_user_isolation_matrix():
    from jobpilot_core.db import session_scope

    A_TEXT = "Kubernetes platform engineer, Go microservices, gRPC, Postgres, Terraform"
    async with session_scope() as s:
        a, b = await _mk_user(s, "A"), await _mk_user(s, "B")
        # A: a full record across every leak-surface table
        ja = await _seed_job(s, a, "Senior Backend Engineer", A_TEXT)
        await R.upsert_qualification(s, a, ja, 0.9, "qualified", None, [], [])
        await R.upsert_analysis(s, a, ja, {"required_skills": ["Go"]}, None, "senior")
        await R.upsert_suggestion(s, a, ja, ["Go"], None, None)
        await R.upsert_cv_draft(s, a, ja, None, {"skills": ["Go"]}, {}, None, 0.5)
        sa = await R.create_chat_session(s, a, "A private chat")
        await R.append_message(s, a, sa.id, "user", "A secret question")
        run_a = await R.create_pipeline_run(s, a, "scout")
        await R.finish_pipeline_run(s, a, run_a.id, "success", output_ref='{"new": 1}')
        await _embed_jobs(s, a)
        # B: only its own single job + chat (nothing of A's)
        jb = await _seed_job(s, b, "Marketing Manager", "Brand campaigns, social media, events")
        sb = await R.create_chat_session(s, b, "B chat")
        await R.append_message(s, b, sb.id, "user", "B question")
        await _embed_jobs(s, b)
        a_session_id = sa.id
    try:
        async with session_scope() as s:
            # every B-scoped read returns ONLY B's data — never A's
            assert [j["id"] for j in await R.get_qualified_jobs(s, b)] == [str(jb)]
            assert await R.get_analyses(s, b) == []       # A has one; B sees none
            assert await R.get_suggestions(s, b) == []
            assert await R.get_cv_drafts(s, b) == []
            assert await R.get_pipeline_runs(s, b) == []
            assert await R.count_jobs(s, b) == 1

            # embeddings don't leak: querying with A's OWN text returns only B's job for B
            qv = embeddings.embed_texts(HS, [A_TEXT])[0]
            b_hits = await R.semantic_search_jobs(s, b, qv, limit=10)
            assert [h["id"] for h in b_hits] == [str(jb)]
            a_hits = await R.semantic_search_jobs(s, a, qv, limit=10)
            assert str(ja) in [h["id"] for h in a_hits]    # A can find its own

            # a user cannot read another user's chat session by id
            assert await R.get_messages(s, b, a_session_id) == []
            assert len(await R.get_messages(s, b, sb.id)) == 1

        # erasing A leaves B completely intact
        async with session_scope() as s:
            await R.delete_user_cascade(s, a)
            await s.flush()
            assert (await R.count_user_rows(s, a))["users"] == 0
        async with session_scope() as s:
            assert [j["id"] for j in await R.get_qualified_jobs(s, b)] == [str(jb)]
            assert await R.count_jobs(s, b) == 1
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, b)
