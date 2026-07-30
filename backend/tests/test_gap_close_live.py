"""Phase 6.6 LIVE — the Gap-closer and the attested alignment loop, for real.

Skips cleanly without Postgres + a real ANTHROPIC_API_KEY. Deliberately ONE job, so the
whole file costs one Gap-closer call plus one alignment loop.

Two things only a live run can settle, and they are the two the deterministic suite cannot
touch:

1. **Does the Gap-closer actually cross the line it is allowed to cross?** A prompt that
   says "propose skills the résumé doesn't support" is worthless if the model, trained
   toward caution, keeps refusing. The test asserts at least one proposal marked
   ``in_resume: false`` — i.e. a genuine addition — and that every item carries the
   interview talking point the UI needs.

2. **Does the loop still converge with attested content in it?** This is the failure mode
   flagged during design: an attested claim has no résumé citation, so a Critic that hasn't
   been told reads it as overclaiming, raises an ``integrity_flag``, and ``is_aligned``
   hard-blocks on any flag — every round, forever, silently burning the round cap. The test
   asserts the final critique carries no integrity flag and the attested items survived.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import pytest
from jobpilot_core import cv_fields, gap_closer, repositories
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

_LIVE_PORTS = {"db": 8761, "config": 8763, "resume": 8762, "pipeline": 8765}


@pytest.fixture(scope="session")
def live_stack(tmp_path_factory):
    if not (_db_reachable() and _has_key()):
        pytest.skip("live test needs Postgres + a real ANTHROPIC_API_KEY")
    _apply_schema()
    env = {**os.environ, "STORAGE_BACKEND": "local",
           "STORAGE_LOCAL_DIR": str(tmp_path_factory.mktemp("live66_storage"))}
    modules = {"db": "db_mcp.server", "config": "config_mcp.server",
               "resume": "resume_mcp.server", "pipeline": "pipeline_mcp.server"}
    urls = {n: f"http://127.0.0.1:{_LIVE_PORTS[n]}/mcp" for n in modules}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{modules[n]}:app", "--port",
             str(_LIVE_PORTS[n]), "--log-level", "warning"], cwd=str(MCP_DIR), env=env)
        for n in modules
    ]
    saved = {a: getattr(S, a) for a in
             ("db_mcp_url", "config_mcp_url", "resume_mcp_url", "pipeline_mcp_url")}
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


# A résumé with NO Terraform and NO Kafka anywhere — so the posting's demand for them is a
# real gap the honest Enhancer would refuse to put in skills_to_add.
BASE = {
    "contact": {"name": "Jane Doe", "email": "jane@x.ie", "phone": "+353 1 555 0100",
                "location": "Dublin", "links": ["github.com/jane"]},
    "summary": "Backend software engineer with cloud experience.",
    "skills": ["Python", "Go", "PostgreSQL", "REST", "Docker"],
    "experience": [
        {"title": "Software Engineer", "company": "Acme", "dates": "2019–2023",
         "bullets": ["Built REST APIs in Go serving internal teams",
                     "Ran services on Docker and AWS ECS",
                     "Owned PostgreSQL schemas and migrations"]},
        {"title": "Junior Developer", "company": "Beta Ltd", "dates": "2017–2019",
         "bullets": ["Maintained internal Python tooling"]},
    ],
    "education": [{"text": "BSc Computer Science, Trinity College Dublin, 2017"}],
}
SNAP = cv_fields.extract_locked_snapshot(BASE)


async def _seed(uid) -> str:
    now = datetime.now(timezone.utc)
    async with session_scope() as s:
        await repositories.create_resume(s, uid, "s3://orig", "Jane Doe CV", BASE, now, now)
        jobs = await repositories.upsert_jobs(s, uid, [JobIn.model_validate(normalize_raw({
            "connector": "greenhouse:stripe", "source": "greenhouse:stripe",
            "source_type": "ats", "company": "Stripe", "title": "Senior Platform Engineer",
            "location": "Remote", "url": f"https://x/{uuid.uuid4().hex}",
            "description": ("Platform role: deep Terraform for multi-account AWS, Kafka "
                            "event streaming at scale, Kubernetes, Go, gRPC, CI/CD."),
            "description_completeness": "full"}).model_dump())])
        jid = jobs[0].id
        await repositories.upsert_analysis(s, uid, jid, {
            "required_skills": ["Terraform", "Kafka", "Kubernetes", "Go", "gRPC"],
            "keywords": ["platform", "infrastructure as code", "event streaming"],
            "seniority": "senior",
            "tech_stack": ["Terraform", "Kafka", "Kubernetes", "Go"],
            "gaps_vs_resume": ["Terraform", "Kafka", "Kubernetes", "gRPC"]},
            {"gaps": ["Terraform", "Kafka", "Kubernetes", "gRPC"]}, "senior")
        await repositories.upsert_qualification(s, uid, jid, 0.52, "qualified", None, [], [])
    return str(jid)


async def _mk_user():
    async with session_scope() as s:
        return (await repositories.create_user(
            s, f"p66l_{uuid.uuid4().hex}@ex.com", hash_password("x"))).id


async def _cleanup(uid):
    async with session_scope() as s:
        await repositories.delete_user_cascade(s, uid)


@requires_live
async def test_live_gap_close_then_attested_alignment(live_stack):
    from app.cv_align_runner import run_align
    from app.gap_close_runner import run_gap_close

    uid = await _mk_user()
    try:
        jid = await _seed(uid)

        # ---------------- (1) the Gap-closer, for real ---------------- #
        summary = await run_gap_close(uid, [jid])
        assert summary["proposed"] == 1, summary
        gc = summary["jobs"][jid]

        print("\n=== GAP-CLOSER PROPOSALS ===")
        for kind in gap_closer._KINDS:
            for it in gc[kind]:
                body = it.get("skill") or it.get("after") or it.get("text")
                print(f"[{it['id']}] ({kind}) {body}")
                print(f"      closes: {it.get('closes')}")
                print(f"      prep:   {it.get('interview_prep')}")

        skills = gc["claimed_skills"]
        assert skills, "the gap-closer proposed no skills at all"
        # THE point of this agent: it must be willing to propose beyond the résumé.
        additions = [s for s in skills if s.get("in_resume") is False]
        assert additions, (
            "no proposal was marked in_resume=false — the agent stayed inside the résumé, "
            "which is the honest Enhancer's job, not this one")
        # Every item must carry the commitment the UI shows before the user vouches.
        for kind in gap_closer._KINDS:
            for it in gc[kind]:
                assert (it.get("interview_prep") or "").strip(), f"{kind} {it['id']} has no prep"
                assert it["accepted"] is None, "proposals must arrive unaccepted"
        # New bullets must target roles that actually exist (role_ref is resolvable).
        companies = {r["company"].lower() for r in BASE["experience"]}
        for nb in gc["new_bullets"]:
            assert (nb.get("role_ref") or "").lower() in companies, (
                f"new bullet targets unknown employer {nb.get('role_ref')!r}")

        # ---------------- (2) the user accepts a subset ---------------- #
        sid = (await _call(live_stack["db"], uid, "get_suggestions", {}))["suggestions"][0]["id"]
        take_skill = additions[0]
        patches = [{"kind": "claimed_skills", "id": take_skill["id"], "accepted": True}]
        take_bullet = gc["new_bullets"][0] if gc["new_bullets"] else None
        if take_bullet:
            patches.append({"kind": "new_bullets", "id": take_bullet["id"], "accepted": True})
        # Reject everything else, so the test proves selectivity, not bulk acceptance.
        for kind in gap_closer._KINDS:
            for it in gc[kind]:
                if not any(p["id"] == it["id"] and p["kind"] == kind for p in patches):
                    patches.append({"kind": kind, "id": it["id"], "accepted": False})
        await _call(live_stack["db"], uid, "set_gap_close_items",
                    {"suggestion_id": sid, "items": patches})

        row = (await _call(live_stack["db"], uid, "get_suggestions", {}))["suggestions"][0]
        acc = gap_closer.accepted_items(row["gap_close_json"])
        assert [s["skill"] for s in acc["claimed_skills"]] == [take_skill["skill"]]

        # ---------------- (3) the real alignment loop, attested ---------------- #
        # Diagnostic: read the accepted set back through the SAME path run_align uses
        # (mcp_client, not the test's direct MCP client) — a mismatch between the two is
        # exactly the kind of thing that silently empties the attested set.
        from app.cv_align_runner import _attested_by_job
        via_runner = await _attested_by_job(uid)
        print("\n=== ATTESTED AS THE RUNNER SEES IT ===")
        print("keys:", list(via_runner))
        print("for this job:", json.dumps(via_runner.get(jid), indent=2, default=str)[:900])

        result = await run_align(uid, [jid])
        assert result["tailored"] == 1, result
        assert result["attested"] == 1, "the loop did not pick up the accepted proposals"

        drafts = (await _call(live_stack["db"], uid, "get_cv_drafts", {}))["drafts"]
        draft = drafts[0]
        content, notes = draft["tailored_content_json"], draft["critic_notes_json"]

        print("\n=== RESULTING DRAFT ===")
        print("aligned:", notes.get("aligned"), "score:", notes.get("alignment_score"),
              "rounds:", notes.get("rounds_run"), "of", notes.get("max_rounds"))
        print("integrity_flags:", notes.get("integrity_flags"))
        print("skills:", content.get("skills"))
        print("summary:", content.get("summary"))
        for r in content.get("experience") or []:
            print(f"  {r['title']} @ {r['company']} ({r['dates']})")
            for b in r.get("bullets") or []:
                print("    •", b)

        # (a) the accepted skill is on the CV, labelled as the candidate's own word
        assert take_skill["skill"] in (content.get("skills") or []), (
            f"attested skill {take_skill['skill']!r} was dropped from the draft")
        basis = {e["skill"]: e["basis"] for e in content.get("skill_evidence") or []}
        assert basis.get(take_skill["skill"]) == cv_fields.USER_CONFIRMED

        # (b) THE convergence check — the fix under test. A Critic that wasn't told about
        #     attested content flags it as overclaiming, and any flag hard-blocks is_aligned.
        assert not (notes.get("integrity_flags") or []), (
            f"Critic flagged attested content: {notes.get('integrity_flags')} — the loop "
            f"can never converge while this happens")

        # (c) the accepted bullet landed under its named role, and nowhere else
        if take_bullet:
            target = (take_bullet.get("role_ref") or "").lower()
            for r in content["experience"]:
                bullets = " ".join(r.get("bullets") or []).lower()
                if r["company"].lower() == target:
                    assert take_bullet["text"].lower()[:40] in bullets, "attested bullet missing"
                else:
                    assert take_bullet["text"].lower()[:40] not in bullets, "bullet leaked roles"

        # (d) rejected proposals never reached the document
        rejected = [s["skill"] for s in skills if s["id"] != take_skill["id"]]
        for skill in rejected:
            if skill.lower() in {s.lower() for s in BASE["skills"]}:
                continue  # already genuinely on the résumé; its presence proves nothing
            assert skill not in (content.get("skills") or []), (
                f"rejected proposal {skill!r} reached the CV")

        # (e) attestation buys content freedom, never structural freedom
        assert content["contact"] == BASE["contact"]
        assert content["education"] == BASE["education"]
        for i, locked in enumerate(SNAP["experience"]):
            got = content["experience"][i]
            assert (got["title"], got["company"], got["dates"]) == (
                locked["title"], locked["company"], locked["dates"])

        # (f) the audit trail records what the user personally guaranteed
        att = notes.get("user_attested") or {}
        assert take_skill["skill"] in (att.get("skills") or [])
        assert "citation" in (att.get("note") or "").lower()
    finally:
        await _cleanup(uid)
