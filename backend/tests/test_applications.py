"""Apply agent, stage 4 — the application log: what was actually sent, and with which CV.

Every other table in the schema records what JobPilot *proposed*. This one records what
the user *did*, which puts a different kind of bug in scope: a wrong row here does not
produce a worse suggestion, it tells someone they have already applied somewhere when
they have not, or hides that one employer is holding two different accounts of their
career. The tests are shaped around the three ways that goes wrong.

* **Over-counting.** The extension records once per step of a multi-step form, so the
  write has to be idempotent per job or a Workday application lands as five.
* **Losing the CV.** The CV field is on step one; later steps legitimately report no CV.
  A naive last-write-wins would demote a tailored application to "no CV" halfway through.
* **Losing the record.** Jobs and drafts are both deletable — the Concierge can bin every
  disqualified job in one call — and the application has to outlive both.

Pure-logic tests run offline; the rest skip cleanly without Postgres.
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import _db_reachable


# --------------------------------------------------------------------------- #
# Pure — which CV a submitted form should be logged against
# --------------------------------------------------------------------------- #
def _ctx(draft_id=None):
    return {"cv": {"path": "/x", "tailored": bool(draft_id), "draft_id": draft_id}}


def _reported(**kw):
    from backend.app.main import SubmittedCv

    return SubmittedCv(**kw)


def test_no_attachment_is_recorded_as_no_cv():
    """A form with no CV field, or one the user had already filled themselves, logs
    ``none``. Claiming the tailored CV went out because one merely existed would be the
    system taking credit for a file it never attached."""
    from backend.app.main import _recorded_cv

    assert _recorded_cv(_ctx("d1"), None)["cv_source"] == "none"
    got = _recorded_cv(_ctx("d1"), _reported(attached=False))
    assert got["cv_source"] == "none"
    assert got["cv_draft_id"] is None  # the id must not leak through on a non-attachment


def test_tailored_attachment_names_the_draft():
    from backend.app.main import _recorded_cv

    got = _recorded_cv(_ctx("d1"), _reported(attached=True, source="tailored", draft_id="d1"))
    assert got["cv_source"] == "tailored" and got["cv_draft_id"] == "d1"


def test_tailored_survives_its_draft_being_deleted():
    """Deleting the draft between filling and submitting loses the id, not the fact. The
    employer still received a tailored CV, so demoting the record to "original" would
    misreport what they are holding."""
    from backend.app.main import _recorded_cv

    got = _recorded_cv(_ctx(None), _reported(attached=True, source="tailored", draft_id="gone"))
    assert got["cv_source"] == "tailored"
    assert got["cv_draft_id"] is None
    assert "deleted" in got["cv_label"]


def test_original_attachment_is_never_upgraded_to_tailored():
    """The extension attached the base resume because that is what the plan resolved. A
    draft tailored *after* that point must not rewrite history into "tailored"."""
    from backend.app.main import _recorded_cv

    got = _recorded_cv(_ctx("tailored-since"), _reported(attached=True, source="original"))
    assert got["cv_source"] == "original" and got["cv_draft_id"] is None


# --------------------------------------------------------------------------- #
# Endpoint wiring — what /apply/record actually sends, and when it sends nothing
# --------------------------------------------------------------------------- #
JID = "11111111-1111-1111-1111-111111111111"
DID = "22222222-2222-2222-2222-222222222222"


def _fake_mcp(monkeypatch, *, drafts=True):
    """Stand in for both MCP services, capturing every call.

    Exercises the endpoint's own wiring — the tool name it picks and the arguments it
    builds — without a running MCP process, which is the layer the DB tests cannot see.
    """
    from backend.app import main

    calls: list[tuple] = []

    async def fake_db(user_id, tool, args=None):
        calls.append((tool, dict(args or {})))
        if tool == "get_job":
            return {"job": {"id": JID, "title": "Backend Eng", "company": "Acme",
                            "apply_board_slug": "acme"}}
        if tool == "get_cv_drafts":
            return {"drafts": [{"id": DID, "job_id": JID, "pdf_ref": "cv.pdf"}] if drafts else []}
        return {"application": {"id": "logged"}}

    async def fake_config(user_id, tool, args=None):
        calls.append((tool, dict(args or {})))
        return {"banked": 0}

    monkeypatch.setattr(main.mcp_client, "db_mcp", fake_db)
    monkeypatch.setattr(main.mcp_client, "config_mcp", fake_config)
    return calls


def _body(**kw):
    from backend.app.main import ApplyRecordRequest

    kw.setdefault("job_id", JID)
    kw.setdefault("answers", [{"question": "Why us?", "value": "Payments", "edited": True}])
    return ApplyRecordRequest.model_validate(kw)


class _User:
    id = uuid.UUID("33333333-3333-3333-3333-333333333333")


async def test_record_endpoint_logs_the_application_with_the_tailored_draft(monkeypatch):
    from backend.app import main

    calls = _fake_mcp(monkeypatch)
    out = await main.record_application(
        _body(cv={"attached": True, "source": "tailored", "draft_id": DID}), current_user=_User())

    logged = [args for tool, args in calls if tool == "record_application"]
    assert len(logged) == 1
    a = logged[0]
    assert a["job_id"] == JID and a["company"] == "Acme" and a["job_title"] == "Backend Eng"
    assert a["employer_slug"] == "acme"
    assert a["cv_source"] == "tailored" and a["cv_draft_id"] == DID
    assert a["answers_recorded"] == 1 and a["source"] == "extension"
    # Banking still happens, and its result is still what the caller gets back.
    assert any(tool == "record_application_answers" for tool, _ in calls)
    assert out["application"] == {"id": "logged"}


async def test_a_stray_apply_click_logs_nothing(monkeypatch):
    """The click heuristic that catches JS-driven submits also fires on the "Apply" button
    of a listing page. No answers behind it means no form was filled, so no application is
    claimed — a log that records applications you did not make is worse than no log."""
    from backend.app import main

    calls = _fake_mcp(monkeypatch)
    await main.record_application(_body(answers=[]), current_user=_User())
    assert not [t for t, _ in calls if t == "record_application"]


async def test_a_form_with_no_known_job_logs_nothing(monkeypatch):
    """The extension can fill a form the user navigated to alone. There is no employer to
    attribute that to, and a log of anonymous applications answers no question worth
    asking — the answers are still banked."""
    from backend.app import main

    calls = _fake_mcp(monkeypatch)
    await main.record_application(_body(job_id=None), current_user=_User())
    assert not [t for t, _ in calls if t == "record_application"]
    assert any(t == "record_application_answers" for t, _ in calls)


async def test_a_lost_log_write_never_fails_a_submitted_application(monkeypatch):
    """The user has already pressed Submit; the employer already has the form. Surfacing a
    logging failure as an error would report a successful application as a broken one."""
    from backend.app import main

    _fake_mcp(monkeypatch)

    async def exploding_db(user_id, tool, args=None):
        if tool == "get_job":
            return {"job": {"id": JID, "title": "Backend Eng", "company": "Acme"}}
        if tool == "get_cv_drafts":
            return {"drafts": []}
        raise RuntimeError("db-mcp is down")

    monkeypatch.setattr(main.mcp_client, "db_mcp", exploding_db)
    out = await main.record_application(
        _body(cv={"attached": True, "source": "original"}), current_user=_User())
    assert out["application"] is None
    assert out["banked"] == 0  # the bank result still comes back intact


# --------------------------------------------------------------------------- #
# DB-gated — the log itself
# --------------------------------------------------------------------------- #
requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


async def _mk_user(session):
    import jobpilot_core.repositories as R
    from jobpilot_core.security import hash_password

    user = await R.create_user(session, f"app_{uuid.uuid4().hex}@ex.com", hash_password("x"))
    return user.id


async def _mk_job(session, uid, *, company="Acme", title="Backend Eng", slug=None):
    import jobpilot_core.repositories as R
    from jobpilot_core.schemas import JobIn
    from jobpilot_core.scout import normalize_raw

    raw = normalize_raw({
        "connector": "greenhouse:acme", "source": "greenhouse:acme", "source_type": "ats",
        "company": company, "title": title, "location": "Dublin",
        "url": f"https://x/{uuid.uuid4().hex}",
        "description": "Go, k8s", "description_completeness": "full",
    })
    jobs = await R.upsert_jobs(session, uid, [JobIn.model_validate(raw.model_dump())])
    job = jobs[0]
    if slug:
        job.apply_board_slug = slug
        await session.flush()
    return job.id


@requires_db
async def test_multi_step_form_logs_one_application():
    """Five steps, one application. Without the unique constraint and the fold-in update
    this is the bug that makes the log useless — every Workday form inflating the count."""
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid = await _mk_user(s)
        jid = await _mk_job(s, uid)
    try:
        async with session_scope() as s:
            first = await R.record_application(
                s, uid, job_id=jid, company="Acme", job_title="Backend Eng",
                cv_source="tailored", cv_label="Tailored CV", answers_recorded=6)
            assert first["created"] is True
        async with session_scope() as s:
            # Step two: no CV field on this page, and fewer answers on it.
            second = await R.record_application(
                s, uid, job_id=jid, company="Acme", cv_source="none", answers_recorded=2)
            assert second["created"] is False
        async with session_scope() as s:
            apps = await R.get_applications(s, uid)
            assert len(apps) == 1
            a = apps[0]
            # The CV survives a later step that had none to report.
            assert a["cv_source"] == "tailored" and a["cv_label"] == "Tailored CV"
            # Evidence of the largest step seen, not a running total a double-fired submit
            # could inflate.
            assert a["answers_recorded"] == 6
            assert a["applied_at"] == first["applied_at"]
            assert a["last_recorded_at"] > first["last_recorded_at"]
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)


@requires_db
async def test_application_outlives_the_job_and_the_draft():
    """The Concierge deletes every disqualified job in one call. Someone who applied in
    March, watched the role fall below a re-tuned threshold and then tidied up must not
    silently lose the record — so the snapshots, not the FKs, carry the answer."""
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid = await _mk_user(s)
        jid = await _mk_job(s, uid)
    try:
        async with session_scope() as s:
            d = await R.upsert_cv_draft(
                s, uid, jid, None, {"skills": ["Go"]}, {"experience": []}, None, 0.5)
            did = d.id
        async with session_scope() as s:
            await R.record_application(
                s, uid, job_id=jid, company="Acme", job_title="Backend Eng",
                cv_source="tailored", cv_draft_id=did, cv_label="Tailored CV",
                answers_recorded=4)
        async with session_scope() as s:
            await R.delete_jobs(s, uid, [jid])
        async with session_scope() as s:
            apps = await R.get_applications(s, uid)
            assert len(apps) == 1, "deleting the job erased the proof you applied"
            a = apps[0]
            assert a["job_id"] is None and a["cv_draft_id"] is None  # both SET NULL
            # ...but the human-readable facts are still there.
            assert a["company"] == "Acme" and a["job_title"] == "Backend Eng"
            assert a["cv_source"] == "tailored" and a["cv_label"] == "Tailored CV"
        async with session_scope() as s:
            # Orphans stay out of the job-joined view, which exists to decorate live rows.
            assert await R.get_applications_by_job(s, uid) == {}
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)


@requires_db
async def test_company_summary_groups_one_employer_reported_under_two_names():
    """Two roles at one company is ordinary. Seeing them as one company is the hard part:
    connectors report the same employer as "Stripe" and "Stripe, Inc.", and grouping on
    the raw display name is exactly how the duplicate goes unnoticed."""
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid = await _mk_user(s)
        a_id = await _mk_job(s, uid, company="Stripe", title="Backend Eng", slug="stripe")
        b_id = await _mk_job(s, uid, company="Stripe, Inc.", title="Platform Eng", slug="stripe")
        c_id = await _mk_job(s, uid, company="Intercom", title="Backend Eng")
    try:
        async with session_scope() as s:
            await R.record_application(
                s, uid, job_id=a_id, company="Stripe", job_title="Backend Eng",
                employer_slug="stripe", cv_source="tailored", cv_label="Tailored CV",
                answers_recorded=5)
            await R.record_application(
                s, uid, job_id=b_id, company="Stripe, Inc.", job_title="Platform Eng",
                employer_slug="stripe", cv_source="tailored", cv_label="Tailored CV",
                answers_recorded=5)
            await R.record_application(
                s, uid, job_id=c_id, company="Intercom", job_title="Backend Eng",
                cv_source="original", cv_label="Original resume", answers_recorded=5)

        async with session_scope() as s:
            summary = await R.application_company_summary(s, uid)
            assert summary["total_applications"] == 3
            repeats = summary["repeats"]
            assert len(repeats) == 1, "the two Stripe roles did not group as one employer"
            assert repeats[0]["count"] == 2
            # Both were tailored but neither stored a draft id, so nothing distinguishes
            # the files — the count reflects what is actually known, not what is assumed.
            assert repeats[0]["cv_variants"] == 1
            assert {a["job_title"] for a in repeats[0]["applications"]} == {
                "Backend Eng", "Platform Eng"}
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)


@requires_db
async def test_distinct_drafts_count_as_distinct_cvs():
    """Two *different* tailored CVs at one employer is the case worth interrupting for,
    and it is only visible because a draft id identifies the file. ``cv_source`` alone
    cannot tell two tailored CVs apart."""
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid = await _mk_user(s)
        a_id = await _mk_job(s, uid, company="Stripe", title="Backend Eng", slug="stripe")
        b_id = await _mk_job(s, uid, company="Stripe", title="Platform Eng", slug="stripe")
    try:
        async with session_scope() as s:
            da = (await R.upsert_cv_draft(
                s, uid, a_id, None, {"skills": ["Go"]}, {"experience": []}, None, 0.5)).id
            db = (await R.upsert_cv_draft(
                s, uid, b_id, None, {"skills": ["Rust"]}, {"experience": []}, None, 0.5)).id
        async with session_scope() as s:
            for jid, did in ((a_id, da), (b_id, db)):
                await R.record_application(
                    s, uid, job_id=jid, company="Stripe", employer_slug="stripe",
                    cv_source="tailored", cv_draft_id=did, cv_label="Tailored CV",
                    answers_recorded=5)
        async with session_scope() as s:
            summary = await R.application_company_summary(s, uid)
            assert summary["repeats"][0]["cv_variants"] == 2
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)


@requires_db
async def test_applications_join_onto_the_dashboard_without_duplicating_jobs():
    """``get_qualified_jobs`` gained an outer join, and an outer join is how a job list
    silently doubles. The unique constraint is what keeps it safe; this is the test that
    would catch its removal."""
    import jobpilot_core.repositories as R
    from jobpilot_core.db import session_scope

    async with session_scope() as s:
        uid = await _mk_user(s)
        jid = await _mk_job(s, uid)
    try:
        async with session_scope() as s:
            await R.record_application(
                s, uid, job_id=jid, company="Acme", cv_source="original",
                cv_label="Original resume", answers_recorded=3)
            await R.record_application(  # a second step of the same form
                s, uid, job_id=jid, company="Acme", cv_source="none", answers_recorded=1)
        async with session_scope() as s:
            jobs = await R.get_qualified_jobs(s, uid)
            assert len(jobs) == 1
            assert jobs[0]["application"]["cv_source"] == "original"
        async with session_scope() as s:
            # Erasure takes applications with everything else.
            await R.delete_user_cascade(s, uid)
            await s.flush()
            assert (await R.count_user_rows(s, uid))["applications"] == 0
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)
