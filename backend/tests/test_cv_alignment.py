"""CV alignment — the evidence gate, the exit test, single-pass mode, and the loop.

Deterministic: both agents are stubbed, so this proves the *control logic and the
integrity gate*, not model quality (that's the key-gated live test). The bars:

  * an added skill is admitted only against a citation that is really in the resume —
    the freedom to re-label genuine experience never becomes freedom to invent it;
  * ``finalize`` is honest by construction whatever the models emit;
  * single pass (``cv_max_rounds = 0``, the default) calls the Rewriter exactly once,
    never calls the Critic, and — the point of the mode's safety — still ships a draft
    that cleared the same code gate as any looped one;
  * the loop stops the moment the Critic says aligned, keeps going when it doesn't,
    and returns the BEST round rather than the last when it never clears the bar.
"""

from __future__ import annotations

import pytest
from jobpilot_core import cv_critic, cv_fields, cv_loop
from jobpilot_core.llm import LLMResult, LLMUsage
from jobpilot_core.settings import get_settings

S = get_settings()

BASE = {
    "contact": {"name": "Jane Doe", "email": "jane@x.ie", "location": "Dublin"},
    "summary": "Backend engineer.",
    "skills": ["Python", "Docker", "PostgreSQL"],
    "experience": [
        {"title": "Software Engineer", "company": "Acme", "dates": "2019–2023",
         "bullets": ["Built REST APIs in Python", "Ran services on Docker and AWS ECS",
                     "Built nightly reporting pipelines into PostgreSQL"]},
    ],
    "education": [{"text": "BSc Computer Science, Trinity College Dublin, 2017"}],
    "certifications": ["AWS Certified Developer"],
}
SNAP = cv_fields.extract_locked_snapshot(BASE)


def _draft(**over):
    d = {
        "contact": BASE["contact"],
        "summary": "Platform engineer.",
        "skills": ["Python", "Docker"],
        "experience": [{"title": "Software Engineer", "company": "Acme", "dates": "2019–2023",
                        "bullets": ["Built REST APIs in Python"]}],
        "education": BASE["education"],
        "skill_evidence": [],
    }
    d.update(over)
    return d


# --------------------------------------------------------------------------- #
# The evidence gate — what may be added to a CV, and on what basis
# --------------------------------------------------------------------------- #
def test_base_skills_need_no_citation():
    res = cv_fields.validate_skill_evidence(_draft(), BASE)
    assert res["ok"] and not res["rejected"]


@pytest.mark.parametrize("skill,basis,quote", [
    # entailed by named work: the resume names Docker + ECS
    ("Container orchestration", "adjacent", "Ran services on Docker and AWS ECS"),
    # the same competence under the job's vocabulary
    ("ETL", "transferable", "Built nightly reporting pipelines into PostgreSQL"),
    # in the resume, just not in the skills list
    ("REST", "explicit", "Built REST APIs in Python"),
])
def test_added_skill_admitted_with_real_citation(skill, basis, quote):
    draft = _draft(skills=["Python", skill],
                   skill_evidence=[{"skill": skill, "basis": basis, "evidence": quote}])
    res = cv_fields.validate_skill_evidence(draft, BASE)
    assert res["ok"], res["rejected"]
    assert skill in [a["skill"] for a in res["admitted"]]
    assert skill in cv_fields.apply_evidence_gate(draft, BASE)["skills"]


def test_added_skill_without_citation_is_rejected():
    draft = _draft(skills=["Python", "Kubernetes"])  # no evidence entry at all
    res = cv_fields.validate_skill_evidence(draft, BASE)
    assert not res["ok"]
    assert res["rejected"][0]["skill"] == "Kubernetes"
    assert "no evidence citation" in res["rejected"][0]["reason"]
    assert "Kubernetes" not in cv_fields.apply_evidence_gate(draft, BASE)["skills"]


def test_citation_that_is_not_in_the_resume_is_rejected():
    """The load-bearing case: a plausible-sounding justification for a skill the
    candidate has never touched. The quote is checked against the REAL resume."""
    draft = _draft(
        skills=["Python", "Kubernetes"],
        skill_evidence=[{"skill": "Kubernetes", "basis": "adjacent",
                         "evidence": "Operated production Kubernetes clusters at scale"}])
    res = cv_fields.validate_skill_evidence(draft, BASE)
    assert not res["ok"]
    assert "does not appear in the real resume" in res["rejected"][0]["reason"]
    assert "Kubernetes" not in cv_fields.apply_evidence_gate(draft, BASE)["skills"]


def test_unknown_basis_is_rejected():
    draft = _draft(skills=["Python", "Rust"],
                   skill_evidence=[{"skill": "Rust", "basis": "vibes",
                                    "evidence": "Built REST APIs in Python"}])
    assert not cv_fields.validate_skill_evidence(draft, BASE)["ok"]


def test_finalize_is_honest_by_construction():
    """Whatever the models emit, what leaves finalize() is safe to export: locked facts
    re-imposed, un-evidenced skills gone, original headers carried through."""
    tampered = _draft(
        contact={"name": "Jane Doe", "email": "other@x.ie"},          # touched
        skills=["Python", "Rust"],                                    # Rust un-evidenced
        experience=[{"title": "Senior Engineer", "company": "Acme",   # title inflated
                     "dates": "2015–2023",                            # dates stretched
                     "bullets": ["Built REST APIs in Python"]}],
        education=[{"text": "MSc Computer Science, Trinity College Dublin, 2017"}],
    )
    out = cv_fields.finalize(tampered, BASE, SNAP)

    assert cv_fields.validate_locked(out, SNAP)["ok"]
    assert out["experience"][0]["title"] == "Software Engineer"
    assert out["experience"][0]["dates"] == "2019–2023"
    assert out["contact"]["email"] == "jane@x.ie"
    assert out["education"] == BASE["education"]
    assert "Rust" not in out["skills"]
    # the one modifiable part of a role survives the locked-field re-imposition
    assert out["experience"][0]["bullets"] == ["Built REST APIs in Python"]
    # the header the real CV had survives even though the draft omitted it
    assert out["certifications"] == ["AWS Certified Developer"]


def test_added_skills_reports_what_changed():
    draft = _draft(skills=["Python", "REST"])
    assert cv_fields.added_skills(draft, BASE) == ["REST"]


# --------------------------------------------------------------------------- #
# Critic payload normalisation — model output is not a contract
# --------------------------------------------------------------------------- #
def test_normalize_reassembles_flat_dimension_scores():
    out = cv_critic.normalize({
        "verdict": "aligned", "alignment_score": 0.9,
        "score_keyword_coverage": 0.8, "score_experience_relevance": 0.9,
        "score_seniority_fit": 0.7, "score_evidence_quality": 1.0,
        "score_presentation": 0.85,
        "matched_requirements": ["Python"], "unaddressed_requirements": [],
        "issues": [], "integrity_flags": [], "instructions": "ship it",
    })
    assert out["dimension_scores"]["keyword_coverage"] == 0.8
    assert out["dimension_scores"]["presentation"] == 0.85
    assert out["malformed"] is False


def test_normalize_survives_the_mangled_nested_object_payload():
    """Regression: a nested `dimension_scores` object made the tool call degrade — sub-keys
    hoisted to the top level, parameter markup leaking into the value, `verdict` dropped,
    and lists arriving stringified. Caught by the live test; must never crash the loop."""
    out = cv_critic.normalize({
        "alignment_score": 0.2,
        "dimension_scores": '\n<parameter name="keyword_coverage">0.3',
        "experience_relevance": 0.2, "seniority_fit": 0.3,
        "evidence_quality": 0.1, "presentation": 0.4,
        "matched_requirements": "['Kubernetes']",
        "unaddressed_requirements": "['Rust', 'systems']",
        "issues": [{"severity": "high", "field": "skills",
                    "problem": "Rust fabricated", "fix": "remove it"}],
        "integrity_flags": [], "instructions": "fix the fabrications",
    })
    assert out["verdict"] == "revise"          # missing verdict fails SAFE, never aligned
    assert out["malformed"] is True
    assert out["matched_requirements"] == ["Kubernetes"]   # stringified list recovered
    assert out["unaddressed_requirements"] == ["Rust", "systems"]
    assert out["dimension_scores"]["experience_relevance"] == 0.2  # hoisted key recovered
    assert out["dimension_scores"]["keyword_coverage"] == 0.0      # unreadable -> 0
    assert not cv_critic.is_aligned(out, 0.85)


@pytest.mark.parametrize("payload", [None, {}, {"verdict": "nonsense"}, []])
def test_normalize_never_yields_an_aligned_verdict_from_junk(payload):
    out = cv_critic.normalize(payload if isinstance(payload, dict) else None)
    assert out["verdict"] == "revise" and out["malformed"] is True
    assert not cv_critic.is_aligned(out, 0.85)


# --------------------------------------------------------------------------- #
# The loop's exit test
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("critique,expected", [
    ({"verdict": "aligned", "alignment_score": 0.9, "integrity_flags": []}, True),
    ({"verdict": "aligned", "alignment_score": 0.8, "integrity_flags": []}, False),   # under bar
    ({"verdict": "revise", "alignment_score": 0.95, "integrity_flags": []}, False),   # not aligned
    ({"verdict": "aligned", "alignment_score": 0.95,
      "integrity_flags": ["overclaims Kubernetes"]}, False),                          # flagged
    ({}, False),
])
def test_is_aligned(critique, expected):
    assert cv_critic.is_aligned(critique, 0.85) is expected


# --------------------------------------------------------------------------- #
# The loop — stubbed agents, real control flow
# --------------------------------------------------------------------------- #
JOB = {"id": "j1", "title": "Platform Engineer", "company": "Stripe"}
ANALYSIS = {"required_skills": ["Python", "Docker"], "keywords": ["platform"],
            "seniority": "senior", "tech_stack": ["Docker"], "gaps_vs_resume": []}


def _result(data):
    return LLMResult(data=data, usage=LLMUsage(model="stub", input_tokens=10, output_tokens=5))


def _stub(monkeypatch, scores: list[float], rewrites: list[dict] | None = None):
    """Critic yields ``scores`` in order; the rewriter returns successive drafts."""
    calls = {"rewrite": 0, "critique": 0}

    async def fake_rewrite(*a, **kw):
        i = calls["rewrite"]
        calls["rewrite"] += 1
        drafts = rewrites or [_draft()]
        return _result(drafts[min(i, len(drafts) - 1)])

    async def fake_critique(*a, **kw):
        i = calls["critique"]
        calls["critique"] += 1
        score = scores[min(i, len(scores) - 1)]
        return _result({
            "verdict": "aligned" if score >= S.cv_alignment_threshold else "revise",
            "alignment_score": score,
            "dimension_scores": {}, "matched_requirements": [],
            "unaddressed_requirements": [], "issues": [], "integrity_flags": [],
            "instructions": "",
        })

    monkeypatch.setattr(cv_loop, "rewrite_cv", fake_rewrite)
    monkeypatch.setattr(cv_loop, "critique_cv", fake_critique)
    return calls


@pytest.fixture
def loop_mode(monkeypatch):
    """Turn the Critic loop on. Single pass is the default, so a test of the loop's control
    flow must ask for it — and a test that forgets fails loudly rather than silently
    asserting against a mode that no longer runs."""
    monkeypatch.setattr(S, "cv_max_rounds", 4)


# --------------------------------------------------------------------------- #
# Single pass (cv_max_rounds = 0) — the default
# --------------------------------------------------------------------------- #
async def test_single_pass_calls_the_rewriter_once_and_the_critic_never(monkeypatch):
    calls = _stub(monkeypatch, [0.92])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)

    assert calls["rewrite"] == 1
    assert calls["critique"] == 0          # the whole point: one LLM call per job
    assert out["rounds"] == []


async def test_single_pass_reports_no_verdict_rather_than_a_failed_one(monkeypatch):
    """`aligned` is None, never False: nothing graded this draft, which is a different
    claim from grading it unaligned. A False here would render as 'did not reach the bar'."""
    _stub(monkeypatch, [0.92])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)

    assert out["aligned"] is None
    assert out["alignment_score"] is None
    assert out["critique"] == {}


async def test_single_pass_output_passed_the_code_gate(monkeypatch):
    """Dropping the Critic must cost judgement, not integrity — the deterministic gate is
    what stops invention, and it still runs with no Critic behind it."""
    dishonest = _draft(skills=["Python", "Kubernetes"],  # un-evidenced
                       experience=[{"title": "Principal Engineer", "company": "Acme",
                                    "dates": "2010–2023", "bullets": ["x"]}])
    _stub(monkeypatch, [0.95], rewrites=[dishonest])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)

    assert "Kubernetes" not in out["cv"]["skills"]              # invented skill dropped
    assert out["cv"]["experience"][0]["title"] == "Software Engineer"   # locked title back
    assert out["cv"]["experience"][0]["dates"] == "2019–2023"
    assert out["code_findings"]["rejected_skills"] == ["Kubernetes"]


# --------------------------------------------------------------------------- #
# The loop (cv_max_rounds > 0)
# --------------------------------------------------------------------------- #
async def test_loop_stops_as_soon_as_aligned(monkeypatch, loop_mode):
    calls = _stub(monkeypatch, [0.92])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)

    assert out["aligned"] is True
    assert out["alignment_score"] == 0.92
    assert len(out["rounds"]) == 1
    assert calls["rewrite"] == 1 and calls["critique"] == 1  # no wasted second rewrite


async def test_loop_rewrites_until_aligned(monkeypatch, loop_mode):
    calls = _stub(monkeypatch, [0.40, 0.70, 0.90])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)

    assert out["aligned"] is True
    assert len(out["rounds"]) == 3
    assert calls["rewrite"] == 3  # the initial draft + one revision per failed round
    assert [r["alignment_score"] for r in out["rounds"]] == [0.40, 0.70, 0.90]


async def test_loop_respects_the_round_cap_and_returns_the_best_round(monkeypatch, loop_mode):
    """Never clears the bar: stop at the cap, report aligned=False, and hand back the
    highest-scoring draft — not whatever the last (weaker) round produced."""
    monkeypatch.setattr(S, "cv_max_rounds", 3)
    best = _draft(summary="the good one")
    _stub(monkeypatch, [0.50, 0.80, 0.60], rewrites=[_draft(), best, _draft(summary="worse")])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)

    assert out["aligned"] is False
    assert len(out["rounds"]) == 3
    assert out["alignment_score"] == 0.80
    assert out["cv"]["summary"] == "the good one"


async def test_loop_output_passed_the_code_gate(monkeypatch, loop_mode):
    """The Critic never sees, and the caller never receives, an un-gated draft."""
    dishonest = _draft(skills=["Python", "Kubernetes"],  # un-evidenced
                       experience=[{"title": "Principal Engineer", "company": "Acme",
                                    "dates": "2010–2023", "bullets": ["x"]}])
    _stub(monkeypatch, [0.95], rewrites=[dishonest])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)

    assert "Kubernetes" not in out["cv"]["skills"]
    assert out["cv"]["experience"][0]["title"] == "Software Engineer"
    assert out["cv"]["experience"][0]["dates"] == "2019–2023"
    assert out["rounds"][0]["code_rejected_skills"] == ["Kubernetes"]


# --------------------------------------------------------------------------- #
# The persisted review trail — what the reviewer is shown about an ungraded draft
# --------------------------------------------------------------------------- #
async def test_single_pass_notes_omit_the_critic_fields_rather_than_zeroing_them(monkeypatch):
    """The UI hides each alignment field on `!= null`, so a single-pass draft must arrive
    with them ABSENT. Writing 0/False instead would render 'best 0%' and 'did not reach the
    bar' — asserting the Critic judged it harshly when no Critic ran at all."""
    from app.cv_align_runner import _notes

    _stub(monkeypatch, [0.92])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)
    notes = _notes(out, out["cv"], BASE, S)

    assert notes["single_pass"] is True
    for absent in ("aligned", "alignment_score", "rounds_run", "max_rounds", "threshold",
                   "final_verdict", "dimension_scores", "integrity_flags"):
        assert absent not in notes, f"{absent} must be omitted, not zeroed, on a single pass"
    # The deterministic findings are evidence, not opinion — they survive the mode change.
    assert notes["code_rejected_skills"] == []
    assert "added_skills" in notes and "skill_evidence" in notes


async def test_loop_notes_still_carry_the_full_alignment_trail(monkeypatch, loop_mode):
    from app.cv_align_runner import _notes

    _stub(monkeypatch, [0.92])
    out = await cv_loop.align_cv(BASE, SNAP, JOB, ANALYSIS, S)
    notes = _notes(out, out["cv"], BASE, S)

    assert "single_pass" not in notes
    assert notes["aligned"] is True and notes["alignment_score"] == 0.92
    assert notes["rounds_run"] == 1 and notes["threshold"] == S.cv_alignment_threshold
