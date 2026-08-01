"""Phase 6.5 LIVE tests — the real Rewriter⇄Critic alignment loop (Sonnet) and the
Concierge chat approval, over the real MCP boundary. Skips cleanly unless a real
ANTHROPIC_API_KEY + Postgres are present.

Covers the acceptance bars that need a live agent, at minimum cost (short cached turns):
  * the full flow: align a selected job → the draft is honest (locked fields
    byte-identical, every skill evidence-backed, the job's real keyword surfaced) →
    approve "by chat" → Tailor an A4 Irish/ATS-safe, machine-readable PDF → re-qualify →
    the draft carries an after_score (bars 1,2,4,5,6,7);
  * the Critic refuses a planted fabrication / touched locked field, and the code gate
    strips them regardless (bar 3).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import pytest
from jobpilot_core import cv_critic, cv_fields, repositories, tailor
from jobpilot_core.db import session_scope
from jobpilot_core.schemas import JobIn
from jobpilot_core.scout import normalize_raw
from jobpilot_core.security import hash_password, mint_mcp_context_token
from jobpilot_core.settings import get_settings
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .conftest import MCP_DIR, _apply_schema, _db_reachable, _wait_port

S = get_settings()


def _has_key() -> bool:
    return (S.anthropic_api_key or "").strip().startswith("sk-ant-")


requires_live = pytest.mark.skipif(
    not (_db_reachable() and _has_key()),
    reason="live test needs Postgres + a real ANTHROPIC_API_KEY",
)

_LIVE_PORTS = {"db": 8751, "config": 8753, "resume": 8752, "pipeline": 8755}


@pytest.fixture(scope="session")
def live_stack(tmp_path_factory):
    if not (_db_reachable() and _has_key()):
        pytest.skip("live test needs Postgres + a real ANTHROPIC_API_KEY")
    _apply_schema()
    env = {**os.environ, "STORAGE_BACKEND": "local",
           "STORAGE_LOCAL_DIR": str(tmp_path_factory.mktemp("live65_storage"))}
    modules = {"db": "db_mcp.server", "config": "config_mcp.server",
               "resume": "resume_mcp.server", "pipeline": "pipeline_mcp.server"}
    urls = {n: f"http://127.0.0.1:{_LIVE_PORTS[n]}/mcp" for n in modules}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{modules[n]}:app", "--port",
             str(_LIVE_PORTS[n]), "--log-level", "warning"], cwd=str(MCP_DIR), env=env)
        for n in modules
    ]
    saved = {a: getattr(S, a) for a in ("db_mcp_url", "config_mcp_url", "resume_mcp_url", "pipeline_mcp_url")}
    S.db_mcp_url, S.config_mcp_url = urls["db"], urls["config"]
    S.resume_mcp_url, S.pipeline_mcp_url = urls["resume"], urls["pipeline"]
    try:
        for n in modules:
            _wait_port(_LIVE_PORTS[n])
        time.sleep(1.2)
        yield urls
    finally:
        for a, v in saved.items():
            setattr(S, a, v)
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pick(res):
    sc = res.structuredContent
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text) if res.content else None


async def _call(url, uid, tool, args=None):
    headers = {"Authorization": f"Bearer {mint_mcp_context_token(uid, S)}"}
    async with streamablehttp_client(url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return _pick(await session.call_tool(tool, args or {}))


BASE = {
    "contact": {"name": "Jane Doe", "email": "jane@x.ie", "phone": "+353 1 555 0100",
                "location": "Dublin", "links": ["github.com/jane"]},
    "summary": "Backend software engineer with cloud experience.",
    "skills": ["Python", "Go", "Kubernetes", "PostgreSQL", "REST"],
    "experience": [
        {"title": "Software Engineer", "company": "Acme", "dates": "2019–2023",
         "bullets": ["Built REST APIs in Go", "Deployed services on Kubernetes",
                     "Owned PostgreSQL schemas"]},
        {"title": "Junior Developer", "company": "Beta Ltd", "dates": "2017–2019",
         "bullets": ["Maintained internal Python tooling"]},
    ],
    "education": [{"text": "BSc Computer Science, Trinity College Dublin, 2017"}],
}


async def _seed(uid) -> str:
    now = datetime.now(timezone.utc)
    async with session_scope() as s:
        await repositories.create_resume(s, uid, "s3://orig", "Jane Doe CV", BASE, now, now)
        jobs = await repositories.upsert_jobs(s, uid, [JobIn.model_validate(normalize_raw({
            "connector": "greenhouse:stripe", "source": "greenhouse:stripe", "source_type": "ats",
            "company": "Stripe", "title": "Senior Platform Engineer", "location": "Remote",
            "url": "https://x/1",
            "description": "Platform role: Kubernetes, Go, gRPC, Terraform, PostgreSQL, CI/CD.",
            "description_completeness": "full"}).model_dump())])
        jid = jobs[0].id
        await repositories.upsert_analysis(s, uid, jid, {
            "required_skills": ["Kubernetes", "Go", "Terraform", "gRPC"],
            "keywords": ["platform", "kubernetes", "microservices", "ci/cd"],
            "seniority": "senior", "tech_stack": ["Kubernetes", "Go", "PostgreSQL"],
            "gaps_vs_resume": ["Terraform"]}, {"gaps": ["Terraform"]}, "senior")
        await repositories.upsert_qualification(s, uid, jid, 0.55, "qualified", None, [], [])
    return str(jid)


async def _mk_user():
    async with session_scope() as s:
        return (await repositories.create_user(s, f"p65l_{uuid.uuid4().hex}@ex.com", hash_password("x"))).id


async def _cleanup(uid):
    async with session_scope() as s:
        await repositories.delete_user_cascade(s, uid)


# --------------------------------------------------------------------------- #
# The full honest flow: tailor → approve by chat → export → re-qualify
# --------------------------------------------------------------------------- #
@requires_live
async def test_live_tailor_review_export_requalify(live_stack, monkeypatch):
    # Force the Critic loop on: single pass is the product default and is covered
    # deterministically in test_cv_alignment.py, so the paid live run is spent on the mode
    # with the moving parts — real Rewriter⇄Critic rounds against a real model.
    monkeypatch.setattr(S, "cv_max_rounds", 4)
    from app.concierge_runner import run_turn
    from app.cv_align_runner import run_align
    from app.cv_export_runner import run_export
    from app.requalify_runner import run_requalify

    uid = await _mk_user()
    try:
        jid = await _seed(uid)

        # (1) align the selected job — real Rewriter⇄Critic loop
        summary = await run_align(uid, [jid])
        assert summary["tailored"] == 1, summary
        drafts = (await _call(live_stack["db"], uid, "get_cv_drafts", {}))["drafts"]
        assert len(drafts) == 1
        draft = drafts[0]
        tc, snap = draft["tailored_content_json"], draft["locked_snapshot_json"]

        # (2) the draft is HONEST: locked fields byte-identical, and every skill on it is
        # either in the base resume or backed by a citation that IS in the base resume.
        assert cv_fields.validate_locked(tc, snap)["ok"], cv_fields.validate_locked(tc, snap)
        ev = cv_fields.validate_skill_evidence(tc, BASE)
        assert ev["ok"], f"un-evidenced skills survived the gate: {ev['rejected']}"
        # locked facts specifically preserved
        assert tc["experience"][0]["company"] == "Acme" and tc["experience"][0]["dates"] == "2019–2023"
        assert tc["contact"]["email"] == "jane@x.ie" and tc["education"] == BASE["education"]
        # and the job's REAL keyword was surfaced (Kubernetes is genuinely in the resume)
        assert "kubernetes" in json.dumps(tc).lower(), tc
        assert draft["status"] == "draft_ready" and draft["critic_notes_json"]

        # (3) approve "by chat" via the Concierge (approve is not a paid action -> acts)
        sid = uuid.UUID((await _call(live_stack["db"], uid, "create_chat_session", {}))["id"])
        approved = False
        for _ in range(3):
            await run_turn(uid, sid, "Approve all of my tailored CV drafts for export.")
            d = (await _call(live_stack["db"], uid, "get_cv_draft", {"draft_id": draft["id"]}))["draft"]
            if d["status"] == "approved":
                approved = True
                break
        assert approved, "chat 'approve all' should have approved the draft"

        # (4) export -> Irish/ATS-safe, machine-readable PDF with locked info intact
        exp = await run_export(uid)
        assert exp["exported"] == 1 and exp["files"][0]["pdf_machine_readable"], exp
        d = (await _call(live_stack["db"], uid, "get_cv_draft", {"draft_id": draft["id"]}))["draft"]
        assert d["pdf_ref"] and d["docx_ref"]
        fetched = await _call(live_stack["resume"], uid, "get_cv_file", {"ref": d["pdf_ref"]})
        text = tailor.pdf_text(base64.b64decode(fetched["file_b64"]))
        assert "Jane Doe" in text and "2019–2023" in text  # real selectable text + locked date
        assert tailor.REFERENCES_LINE in text
        assert "date of birth" not in text.lower()

        # (5) re-qualify the tailored CV -> an after_score is recorded (legitimate lift)
        req = await run_requalify(uid)
        assert req["requalified"] == 1, req
        d = (await _call(live_stack["db"], uid, "get_cv_draft", {"draft_id": draft["id"]}))["draft"]
        assert d["after_score"] is not None and d["before_score"] == 0.55
    finally:
        await _cleanup(uid)


# --------------------------------------------------------------------------- #
# Bar 3 — the real Critic catches a planted fabrication / touched locked field
# --------------------------------------------------------------------------- #
@requires_live
async def test_live_critic_catches_planted_fabrication(live_stack):
    from jobpilot_core.cv_critic import critique_cv

    snap = cv_fields.extract_locked_snapshot(BASE)
    analysis = {"required_skills": ["Rust", "Kubernetes"], "keywords": ["systems"],
                "seniority": "senior", "tech_stack": ["Rust"]}
    job = {"title": "Systems Engineer", "company": "Stripe"}
    # a draft with a fabricated skill (Rust — not in the resume) AND a tampered employer date
    bad = {
        "contact": BASE["contact"],
        "summary": "Senior Rust systems engineer.",
        "skills": ["Rust", "Python", "Go", "Kubernetes"],  # Rust fabricated
        "experience": [
            {"title": "Software Engineer", "company": "Acme", "dates": "2015–2023",  # date tampered
             "bullets": ["Built Rust systems"]},  # Rust experience fabricated
            {"title": "Junior Developer", "company": "Beta Ltd", "dates": "2017–2019",
             "bullets": ["Maintained internal Python tooling"]},
        ],
        "education": BASE["education"],
    }
    code_v = cv_fields.validate_tailored(bad, BASE, snap)
    assert not code_v["ok"]  # the validator already flags it

    # (a) the CODE gate is the hard guarantee: Rust has no evidence citation so it is
    # deleted, and the tampered employer date is re-imposed from the snapshot.
    fixed = cv_fields.finalize(bad, BASE, snap)
    assert fixed["experience"][0]["dates"] == "2019–2023", fixed["experience"][0]
    assert "Rust" not in (fixed.get("skills") or []), fixed.get("skills")
    assert cv_fields.validate_locked(fixed, snap)["ok"]

    # (b) the CRITIC is the judgement layer: it must refuse to pass the planted draft.
    findings = {"violations": code_v["violations"], "rejected_skills": code_v["fabricated"]}
    res = await critique_cv(BASE, snap, job, analysis, bad, S, code_findings=findings)
    c = res.data
    assert c["verdict"] == "revise"  # the critic does not pass a fabricated/tampered draft
    assert c["integrity_flags"], "a fabricated Rust claim must raise an integrity flag"
    assert not cv_critic.is_aligned(c, S.cv_alignment_threshold)  # so the loop runs again
