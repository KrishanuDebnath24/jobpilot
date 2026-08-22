"""Phase 6.5 — Tailored CV generation, critique, export & re-qualification tests.

Two layers:

* **Pure/policy** (no DB, no LLM): the integrity spine (locked-field snapshot,
  validation catching drift + fabrication, enforce/drop making a draft honest by
  construction), the deterministic Irish/ATS-safe renderer (A4, machine-readable text,
  Irish section order), the Concierge CV tools' confirmation policy, and the executor's
  trust boundary + routing through a fake MCP call. A deterministic re-qualify shows a
  legitimate score lift (more real keywords surfaced) with no LLM.
* **DB-gated** (skips without Postgres): the cv_draft lifecycle round-trip and erasure.

The live tailor+critic (real Sonnet: keywords surfaced, locked byte-identical, a planted
fabrication caught) + chat approval + export + re-qualify report is in
``test_phase6_5_live.py`` (key-gated).
"""

from __future__ import annotations

import uuid

import pytest
from jobpilot_core import concierge, cv_fields, qualifier, tailor
from jobpilot_core.storage import cv_key, user_prefix

from .conftest import _db_reachable

BASE = {
    "contact": {"name": "Jane Doe", "email": "jane@x.ie", "phone": "+353 1 555 0100",
                "location": "Dublin", "links": ["github.com/jane"]},
    "summary": "Backend engineer.",
    "skills": ["Python", "Go", "Kubernetes", "SQL"],
    "experience": [
        {"title": "Software Engineer", "company": "Acme", "location": "Dublin, Ireland",
         "dates": "2019–2023",
         "bullets": ["Built REST APIs in Python", "Ran Postgres and deployments"]},
        # No location: a resume parsed before the field existed must still render and still
        # validate, so one role deliberately carries none.
        {"title": "Junior Developer", "company": "Beta Ltd", "dates": "2017–2019",
         "bullets": ["Maintained internal tooling"]},
    ],
    "education": [{"text": "BSc Computer Science, Trinity College Dublin, 2017"}],
}


# --------------------------------------------------------------------------- #
# 6.5a/6.5c — locked fields: snapshot, validation, enforcement (the integrity spine)
# --------------------------------------------------------------------------- #
def test_snapshot_captures_the_locked_facts():
    snap = cv_fields.extract_locked_snapshot(BASE)
    assert snap["contact"] == BASE["contact"]
    assert snap["education"] == BASE["education"]
    assert snap["experience"] == [
        {"title": "Software Engineer", "company": "Acme", "location": "Dublin, Ireland",
         "dates": "2019–2023"},
        {"title": "Junior Developer", "company": "Beta Ltd", "location": None,
         "dates": "2017–2019"},
    ]


def test_validate_locked_catches_every_kind_of_drift():
    snap = cv_fields.extract_locked_snapshot(BASE)
    # tamper a date, an employer, a title, contact, and drop a role
    bad = {
        "contact": {**BASE["contact"], "email": "someone-else@x.ie"},
        "skills": BASE["skills"],
        "experience": [
            {"title": "Senior Software Engineer", "company": "Acme Corp", "location": "Berlin",
             "dates": "2018–2023", "bullets": ["…"]},
        ],
        "education": [{"text": "MSc AI, Somewhere, 2018"}],
    }
    v = cv_fields.validate_locked(bad, snap)
    assert not v["ok"]
    joined = " ".join(v["violations"])
    assert "contact" in joined and "education" in joined and "role count" in joined
    assert "dates" in joined and "company" in joined and "title" in joined
    assert "location" in joined  # where a role happened is a fact, not a tailoring choice


def test_validate_catches_fabricated_skill_but_allows_grounded():
    # "Rust" appears nowhere in the base -> fabricated; "REST" is in a bullet -> grounded
    tailored = {"skills": ["Python", "REST", "Rust"], **{k: BASE[k] for k in ("contact", "experience", "education")}}
    v = cv_fields.validate_no_fabricated_skills(tailored, BASE)
    assert v["fabricated"] == ["Rust"]
    assert cv_fields.drop_fabricated_skills(tailored, BASE)["skills"] == ["Python", "REST"]


def test_enforce_locked_makes_a_tampered_draft_honest_by_construction():
    snap = cv_fields.extract_locked_snapshot(BASE)
    tampered = {
        "contact": {"name": "Jane Doe", "email": "WRONG"},
        "summary": "Kubernetes platform engineer.",
        "skills": ["Kubernetes", "Python", "Go", "SQL", "Rust"],  # Rust fabricated
        "experience": [
            {"title": "Senior Engineer", "company": "Acme", "dates": "2015–2023",  # title+date drift
             "bullets": ["Designed REST APIs and Kubernetes platforms"]},
            {"title": "Junior Developer", "company": "Beta Ltd", "dates": "2017–2019",
             "bullets": ["Owned internal tooling"]},
        ],
        "education": [{"text": "TAMPERED"}],
    }
    assert not cv_fields.validate_tailored(tampered, BASE, snap)["ok"]  # caught
    fixed = cv_fields.drop_fabricated_skills(cv_fields.enforce_locked(tampered, snap), BASE)
    v = cv_fields.validate_tailored(fixed, BASE, snap)
    assert v["ok"]  # honest by construction
    assert fixed["experience"][0]["title"] == "Software Engineer"  # locked restored
    assert fixed["experience"][0]["dates"] == "2019–2023"
    assert fixed["education"] == BASE["education"]
    assert "Rust" not in fixed["skills"]
    # the MODIFIABLE bullets survive (tailored phrasing kept)
    assert "Kubernetes" in fixed["experience"][0]["bullets"][0]


# --------------------------------------------------------------------------- #
# 6.5e — deterministic Irish/ATS-safe render + machine-readability
# --------------------------------------------------------------------------- #
def test_pdf_is_a4_and_machine_readable_with_irish_order():
    from pypdf import PdfReader
    import io

    pdf = tailor.render_pdf(BASE)
    reader = PdfReader(io.BytesIO(pdf))
    box = reader.pages[0].mediabox
    assert abs(float(box.height) - 841.89) < 2 and abs(float(box.width) - 595.27) < 2  # A4 points

    text = tailor.pdf_text(pdf)
    assert len(text) >= 50  # real selectable text, not an image
    for token in ("Jane Doe", "2019–2023", "Trinity College Dublin", tailor.REFERENCES_LINE):
        assert token in text, token
    # Irish section order, in Jake's capitalised headers
    order = [text.index(s) for s in ("PROFESSIONAL SUMMARY", "TECHNICAL SKILLS",
                                     "EXPERIENCE", "EDUCATION")]
    assert order == sorted(order)
    # no US-style photo/DOB fields are ever emitted
    low = text.lower()
    assert "date of birth" not in low and "marital status" not in low


def test_role_location_is_rendered_and_reads_in_order():
    """The two-column entry row is drawn, not tabled, so extraction must still read
    title → dates → employer → location — the order a human reads it."""
    text = tailor.pdf_text(tailor.render_pdf(BASE))
    assert "Dublin, Ireland" in text  # the location that used to be dropped entirely
    assert text.index("Software Engineer") < text.index("Acme") < text.index("Dublin, Ireland")
    # The role with no location still renders its employer, and invents nothing.
    assert "Beta Ltd" in text


def test_render_survives_a_resume_parsed_before_location_existed():
    """Every stored draft predates the field. None of them may fail or grow a stray blank
    column because of it."""
    legacy = {**BASE, "experience": [{k: v for k, v in role.items() if k != "location"}
                                     for role in BASE["experience"]]}
    out = tailor.render_both(legacy)
    assert out["pdf_machine_readable"]
    text = tailor.pdf_text(out["pdf"])
    assert "Acme" in text and "None" not in text


def test_additional_sections_survive_to_the_page():
    """Projects/Publications are the sections a CV is judged on and the parser used to drop
    them on the floor; prove the render prints them under their own header."""
    cv = {**BASE, "additional_sections": [
        {"heading": "Projects", "items": ["Multi-agent platform | Python, LangGraph"]}]}
    text = tailor.pdf_text(tailor.render_pdf(cv))
    assert "PROJECTS" in text and "Multi-agent platform" in text


def test_education_dates_move_to_the_right_column_only_when_unambiguous():
    assert tailor._split_trailing_dates(
        "Maynooth University | M.Sc. Robotics — Maynooth, Ireland, Sep. 2024 - Sep. 2025"
    ) == ("Maynooth University | M.Sc. Robotics — Maynooth, Ireland", "Sep. 2024 - Sep. 2025")
    assert tailor._split_trailing_dates("BSc Computer Science, Trinity College Dublin, 2017") == (
        "BSc Computer Science, Trinity College Dublin, 2017", "")  # a lone year is not a range


def test_docx_is_a4_and_readable():
    from docx.shared import Mm

    docx_bytes = tailor.render_docx(BASE)
    text = tailor.docx_text(docx_bytes)
    assert "Jane Doe" in text and "Kubernetes" in text and tailor.REFERENCES_LINE in text
    assert "Dublin, Ireland" in text  # the location column, as a right-aligned tab

    import io
    import docx
    doc = docx.Document(io.BytesIO(docx_bytes))  # A4 within twips-quantization rounding
    assert abs(int(doc.sections[0].page_width) - int(Mm(210))) < 2000
    assert abs(int(doc.sections[0].page_height) - int(Mm(297))) < 2000


def test_render_both_reports_machine_readable():
    out = tailor.render_both(BASE)
    assert out["pdf_machine_readable"] and out["pdf_text_len"] >= 50
    assert len(out["pdf"]) > 500 and len(out["docx"]) > 500


# --------------------------------------------------------------------------- #
# 6.5f — a legitimate score lift is possible with NO LLM (deterministic pass only)
# --------------------------------------------------------------------------- #
def test_requalify_lifts_score_when_more_required_skills_are_matched():
    # Skills are now LLM-graded (matched/required); a tailored CV that surfaces REAL keywords
    # raises `matched_skills`, which raises the skills dimension and the final score. This
    # checks the combine mechanism deterministically (no LLM call): same job/required set,
    # more matched -> higher score.
    cfg = qualifier.default_qualifier_config(BASE["skills"])
    job = {"title": "Kubernetes Platform Engineer", "company": "X",
           "description": "We need Kubernetes, Go, and REST API experience."}
    det = qualifier.score_deterministic(BASE, job, cfg)
    required = ["kubernetes", "go", "rest api"]
    before_llm = {"required_skills": required, "matched_skills": [], "missing_skills": required,
                  "roles_match": 0.5, "reasons": []}
    after_llm = {"required_skills": required, "matched_skills": required, "missing_skills": [],
                 "roles_match": 0.5, "reasons": []}
    before = qualifier.combine(det, before_llm, cfg)["score"]
    after = qualifier.combine(det, after_llm, cfg)["score"]
    assert after > before  # surfacing REAL matching skills legitimately raises the fit


# --------------------------------------------------------------------------- #
# CV files live under the user's storage prefix -> wiped by delete_user_data
# --------------------------------------------------------------------------- #
def test_cv_files_are_under_user_prefix_for_erasure():
    uid = uuid.uuid4()
    key = cv_key(uid, uuid.uuid4(), "pdf")
    assert key.startswith(user_prefix(uid)) and key.endswith(".pdf")


# --------------------------------------------------------------------------- #
# 6.5d — Concierge CV tools: confirmation policy + executor routing (fake call)
# --------------------------------------------------------------------------- #
def test_concierge_cv_tools_confirmation_policy():
    assert {"tailor_cvs", "requalify_cvs"} <= concierge.CONSEQUENTIAL
    assert concierge.authorize("tailor_cvs", False) == "confirm"
    assert concierge.authorize("requalify_cvs", False) == "confirm"
    # approve/reject/export/list/report are NOT paid -> execute directly
    for t in ("approve_cv_drafts", "reject_cv_drafts", "export_cvs", "list_cv_drafts",
              "cv_improvement_report"):
        assert concierge.authorize(t, False) == "execute"


def _executor_with_capture():
    from app.concierge_runner import ConciergeExecutor

    calls: list[tuple] = []

    async def fake_call(user_id, service, tool, args):
        calls.append((service, tool, dict(args)))
        if tool == "get_cv_drafts" and args.get("status") == "draft_ready":
            return {"drafts": [{"id": "d1"}, {"id": "d2"}]}
        if tool == "get_cv_drafts":
            return {"drafts": [
                {"id": "d1", "job_id": "j1", "status": "approved", "before_score": 0.5, "after_score": 0.7},
                {"id": "d2", "job_id": "j2", "status": "approved", "before_score": 0.6, "after_score": 0.6},
            ]}
        if tool == "get_qualified_jobs":
            return {"jobs": [{"id": "j1", "title": "Stripe", "company": "Stripe"},
                             {"id": "j2", "title": "Intercom", "company": "Intercom"}]}
        if tool == "set_cv_draft_status":
            return {"updated": len(args.get("draft_ids") or []), "status": args["status"]}
        return {}

    return ConciergeExecutor(uuid.uuid4(), call=fake_call), calls


async def test_executor_tailor_cvs_requires_confirmation():
    ex, calls = _executor_with_capture()
    result, _ = await ex.execute("tailor_cvs", {"job_ids": ["j1", "j2"]}, confirmed=False)
    assert result["status"] == "confirmation_required"  # a paid run is held for a yes
    assert not calls  # nothing dispatched/enqueued


async def test_executor_approve_all_resolves_draft_ready_ids():
    ex, calls = _executor_with_capture()
    result, _ = await ex.execute("approve_cv_drafts", {"all": True})
    # it looked up draft_ready ids, then set them approved
    tools = [(s, t) for s, t, _ in calls]
    assert ("db", "get_cv_drafts") in tools and ("db", "set_cv_draft_status") in tools
    set_args = [a for s, t, a in calls if t == "set_cv_draft_status"][0]
    assert set(set_args["draft_ids"]) == {"d1", "d2"} and set_args["status"] == "approved"
    assert result["updated"] == 2


async def test_executor_cv_report_computes_deltas():
    ex, _ = _executor_with_capture()
    result, is_data = await ex.execute("cv_improvement_report", {})
    assert result["improved"] == 1 and result["total"] == 2 and is_data
    top = result["report"][0]
    assert top["job_title"] == "Stripe" and top["delta"] == pytest.approx(0.2)


async def test_executor_strips_user_id_on_cv_tools():
    ex, calls = _executor_with_capture()
    await ex.execute("list_cv_drafts", {"user_id": str(uuid.uuid4()), "status": "approved"})
    assert calls[-1][1] == "get_cv_drafts" and "user_id" not in calls[-1][2]


# --------------------------------------------------------------------------- #
# DB-gated — cv_draft lifecycle + erasure
# --------------------------------------------------------------------------- #
requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


async def _mk_user_and_job(session):
    from jobpilot_core.schemas import JobIn
    from jobpilot_core.scout import normalize_raw
    from jobpilot_core.security import hash_password

    uid = (await __import__("jobpilot_core.repositories", fromlist=["create_user"]).create_user(
        session, f"p65_{uuid.uuid4().hex}@ex.com", hash_password("x"))).id
    import jobpilot_core.repositories as R
    jobs = await R.upsert_jobs(session, uid, [JobIn.model_validate(normalize_raw({
        "connector": "greenhouse:acme", "source": "greenhouse:acme", "source_type": "ats",
        "company": "Acme", "title": "Backend Eng", "location": "Remote", "url": "https://x/1",
        "description": "Go, k8s", "description_completeness": "full"}).model_dump())])
    return uid, jobs[0].id


@requires_db
async def test_cv_draft_lifecycle_and_erasure():
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid, jid = await _mk_user_and_job(s)
    try:
        async with session_scope() as s:
            d = await R.upsert_cv_draft(
                s, uid, jid, None, {"skills": ["Go"]}, {"experience": []},
                {"rounds": [{"verdict": "pass"}]}, before_score=0.55)
            did = d.id
        async with session_scope() as s:
            await R.set_cv_draft_status(s, uid, [did], "approved")
            await R.set_cv_draft_export(s, uid, did, "resumes/x/cv/a.pdf", "resumes/x/cv/a.docx")
            await R.set_cv_draft_after_score(s, uid, did, 0.72)
        async with session_scope() as s:
            got = (await R.get_cv_drafts(s, uid))[0]
            assert got["status"] == "approved" and got["after_score"] == 0.72 and got["pdf_ref"]
        async with session_scope() as s:  # re-tailor replaces + resets to review
            await R.upsert_cv_draft(s, uid, jid, None, {"skills": ["Rust"]}, {"experience": []}, None, 0.55)
        async with session_scope() as s:
            got = await R.get_cv_drafts(s, uid)
            assert len(got) == 1 and got[0]["status"] == "draft_ready"
            assert got[0]["after_score"] is None and got[0]["pdf_ref"] is None
        async with session_scope() as s:  # erasure wipes drafts with everything else
            await R.delete_user_cascade(s, uid)
            await s.flush()
            assert (await R.count_user_rows(s, uid))["cv_drafts"] == 0
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)


@requires_db
async def test_delete_cv_drafts_takes_the_draft_and_leaves_the_job():
    """Deleting a CV must not delete the pipeline work behind it: the job, its
    qualification and its suggestions are what a re-tailor needs, and losing them to a
    tidy-up would be unrecoverable."""
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid, jid = await _mk_user_and_job(s)
    try:
        async with session_scope() as s:
            d = await R.upsert_cv_draft(
                s, uid, jid, None, {"skills": ["Go"]}, {"experience": []}, None, 0.5)
            did = d.id
        async with session_scope() as s:
            await R.set_cv_draft_export(s, uid, did, "resumes/u/cv/a.pdf", "resumes/u/cv/a.docx")

        async with session_scope() as s:
            # Refs must be readable BEFORE the delete — afterwards the row is the only
            # record of where the files were, so they would be orphaned in storage.
            refs = await R.cv_file_refs_for_drafts(s, uid, [did])
            assert sorted(refs) == ["resumes/u/cv/a.docx", "resumes/u/cv/a.pdf"]
            assert await R.delete_cv_drafts(s, uid, []) == 0  # empty means nothing, not all
            assert await R.delete_cv_drafts(s, uid, [did]) == 1

        async with session_scope() as s:
            assert await R.get_cv_drafts(s, uid) == []
            assert (await R.count_user_rows(s, uid))["jobs"] == 1  # the job survived
            assert await R.cv_file_refs_for_drafts(s, uid, [did]) == []
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)


@requires_db
async def test_delete_cv_drafts_cannot_reach_another_users_draft():
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        owner, owner_job = await _mk_user_and_job(s)
    async with session_scope() as s:
        other, _ = await _mk_user_and_job(s)
    try:
        async with session_scope() as s:
            did = (await R.upsert_cv_draft(
                s, owner, owner_job, None, {"skills": []}, {"experience": []}, None, 0.5)).id
        async with session_scope() as s:
            assert await R.delete_cv_drafts(s, other, [did]) == 0
            assert await R.cv_file_refs_for_drafts(s, other, [did]) == []
        async with session_scope() as s:
            assert len(await R.get_cv_drafts(s, owner)) == 1  # untouched
    finally:
        for uid in (owner, other):
            async with session_scope() as s:
                await R.delete_user_cascade(s, uid)
