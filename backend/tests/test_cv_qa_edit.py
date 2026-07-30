"""Per-job CV Q&A + editing (Concierge extension) — deterministic.

Covers the zero-token paths (job digest, CV sections, add/remove edits, role+company
resolution) with no model calls. The bars:
  * a job is resolved by role+company, never by id; ambiguity is surfaced, not guessed;
  * the JD digest reshapes the stored analysis (no recompute);
  * removing skills/bullets always works; adding an ungrounded skill is admitted only as
    user_confirmed, and stays on the CV with that auditable basis;
  * every edit is finalized (locked facts enforced) and the draft resets to draft_ready.
"""

from __future__ import annotations

import uuid

import pytest
from jobpilot_core import cv_digest, cv_edit, cv_fields

BASE = {
    "contact": {"name": "Jane Doe", "email": "jane@x.ie"},
    "summary": "Backend engineer.",
    "skills": ["Python", "Docker", "PostgreSQL"],
    "experience": [
        {"title": "Software Engineer", "company": "Acme", "dates": "2019–2023",
         "bullets": ["Built REST APIs in Python", "Ran services on Docker and AWS ECS"]},
    ],
    "education": [{"text": "BSc CS, Trinity College Dublin, 2017"}],
    "certifications": ["AWS Certified Developer"],
}
SNAP = cv_fields.extract_locked_snapshot(BASE)


def _content(**over):
    c = {
        "contact": BASE["contact"], "summary": "Platform engineer.",
        "skills": ["Python", "Docker"],
        "experience": [{"title": "Software Engineer", "company": "Acme", "dates": "2019–2023",
                        "bullets": ["Built REST APIs in Python"]}],
        "education": BASE["education"], "skill_evidence": [],
    }
    c.update(over)
    return c


# --------------------------------------------------------------------------- #
# Job digest — reshapes the stored analysis, no recompute
# --------------------------------------------------------------------------- #
def test_job_digest_from_stored_analysis():
    job = {"title": "Platform Engineer", "company": "Stripe", "location": "Dublin",
           "description": "We need 5+ years building platforms."}
    analysis = {"required_skills": ["Kubernetes", "Go"], "tech_stack": ["Terraform"],
                "keywords": ["platform"], "seniority": "senior",
                "required_certifications": ["CKA"], "gaps_vs_resume": ["Kubernetes"]}
    d = cv_digest.job_digest(job, analysis)
    assert d["role"] == "Platform Engineer @ Stripe"
    assert d["skills_required"] == ["Kubernetes", "Go"]
    assert d["qualifications"] == ["CKA"]
    assert "5+ years" in d["experience_needed"] and "senior" in d["experience_needed"]
    assert d["gaps_vs_your_cv"] == ["Kubernetes"]
    assert d["analyzed"] is True


def test_job_digest_without_analysis_flags_it():
    d = cv_digest.job_digest({"title": "X", "company": "Y", "description": ""}, None)
    assert d["analyzed"] is False and d["skills_required"] == []


def test_cv_sections_tags_skill_basis_and_marks_locked():
    content = _content(skills=["Python", "Kubernetes"],
                       skill_evidence=[{"skill": "Kubernetes", "basis": "user_confirmed",
                                        "evidence": "user confirmed"}])
    s = cv_digest.cv_sections(content, {"title": "Platform", "company": "Stripe"})
    assert s["profile"] == "Platform engineer."
    kmap = {r["skill"]: r["basis"] for r in s["skills"]}
    assert kmap["Kubernetes"] == "user_confirmed"
    assert s["experience"][0]["company"] == "Acme"  # locked facts shown as context


# --------------------------------------------------------------------------- #
# Editing — deterministic, honest by construction
# --------------------------------------------------------------------------- #
def test_remove_skill_and_bullet():
    out = cv_edit.edit_tailored_cv(_content(), BASE, SNAP,
                                   remove_skills=["Docker"],
                                   remove_bullets=["REST APIs"])
    assert "Docker" not in out["content"]["skills"]
    assert out["content"]["experience"][0]["bullets"] == []
    assert any("removed skill 'Docker'" in c for c in out["changes"])


def test_add_grounded_skill_needs_no_confirmation():
    # "AWS" appears in a base bullet ("AWS ECS"), so it's grounded, not user_confirmed
    out = cv_edit.edit_tailored_cv(_content(), BASE, SNAP, add_skills=["AWS"])
    assert "AWS" in out["content"]["skills"]
    ev = {e["skill"]: e["basis"] for e in out["content"].get("skill_evidence") or []}
    assert ev.get("AWS") != cv_fields.USER_CONFIRMED  # grounded, so no user_confirmed tag


def test_add_ungrounded_skill_is_recorded_user_confirmed():
    out = cv_edit.edit_tailored_cv(_content(), BASE, SNAP, add_skills=["Kubernetes"])
    assert "Kubernetes" in out["content"]["skills"]           # survives the gate (user-confirmed)
    ev = {e["skill"]: e["basis"] for e in out["content"]["skill_evidence"]}
    assert ev["Kubernetes"] == cv_fields.USER_CONFIRMED       # and is auditable as such
    assert any("user-confirmed" in c for c in out["changes"])


def test_add_bullet_to_named_role():
    out = cv_edit.edit_tailored_cv(_content(), BASE, SNAP,
                                   add_bullets=[{"text": "Led the Docker migration", "role_ref": "Acme"}])
    assert "Led the Docker migration" in out["content"]["experience"][0]["bullets"]


def test_edit_never_touches_locked_facts():
    tampered = _content(experience=[{"title": "Principal Engineer", "company": "Acme",
                                     "dates": "2010–2023", "bullets": ["x"]}])
    out = cv_edit.edit_tailored_cv(tampered, BASE, SNAP, add_skills=["Python"])
    role = out["content"]["experience"][0]
    assert role["title"] == "Software Engineer" and role["dates"] == "2019–2023"


# --------------------------------------------------------------------------- #
# Role+company resolution in the executor — never asks for an id
# --------------------------------------------------------------------------- #
def _executor(jobs):
    from app.concierge_runner import ConciergeExecutor

    async def fake_call(user_id, service, tool, args):
        if tool == "get_qualified_jobs":
            return {"jobs": jobs}
        return {}

    return ConciergeExecutor(uuid.uuid4(), call=fake_call)


JOBS = [
    {"id": "j1", "title": "Senior Backend Engineer", "company": "Stripe"},
    {"id": "j2", "title": "Platform Engineer", "company": "Intercom"},
    {"id": "j3", "title": "Backend Engineer", "company": "Intercom"},
]


async def test_resolve_by_role_and_company():
    ex = _executor(JOBS)
    job, err = await ex._resolve_single_job({"job_ref": "backend at stripe"})
    assert err is None and job["id"] == "j1"


async def test_resolve_ambiguous_returns_candidates_not_ids():
    ex = _executor(JOBS)
    job, err = await ex._resolve_single_job({"job_ref": "engineer at intercom"})
    assert job is None and err["status"] == "ambiguous"
    assert err["candidates"] and all("@" in c for c in err["candidates"])  # by role@company
    assert "id" not in " ".join(err.get("candidates") or []).lower()


async def test_resolve_no_match_is_honest():
    ex = _executor(JOBS)
    job, err = await ex._resolve_single_job({"job_ref": "data scientist at google"})
    assert job is None and err["status"] == "no_match"


async def test_resolve_uses_selection_when_no_ref():
    from app.concierge_runner import ConciergeExecutor

    async def fake_call(user_id, service, tool, args):
        return {"jobs": JOBS} if tool == "get_qualified_jobs" else {}

    ex = ConciergeExecutor(uuid.uuid4(), call=fake_call, selected_job_ids=["j2"])
    job, err = await ex._resolve_single_job({"use_selected": True})
    assert err is None and job["id"] == "j2"


def test_new_tools_are_not_confirmation_gated():
    from jobpilot_core import concierge
    for t in ("explain_job", "show_cv", "edit_cv", "reword_cv"):
        assert t in concierge.TOOL_NAMES
        assert concierge.authorize(t, confirmed=False) == "execute"  # frictionless
