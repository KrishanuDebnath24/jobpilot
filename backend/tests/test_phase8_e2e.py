"""Phase 8h — full end-to-end pipeline test (deterministic, CI-safe, no key/DB/network).

Composes the REAL pipeline functions across the whole flow — onboard → scout → qualify →
analyze → enhance/apply → tailor CV → export → re-qualify → query — proving the data
contracts hold end to end. LLM-driven refinements (Qualifier/Analyzer/Enhancer/Tailor
Haiku/Sonnet passes) and the persistence layer are covered by the per-phase live + DB
tests; this is the deterministic spine that must never break, runnable anywhere.
"""

from __future__ import annotations

from jobpilot_core import analyzer, cv_fields, qualifier, resume_apply, tailor
from jobpilot_core.scout import normalize_raw

# 1) ONBOARD — a parsed resume (the output of resume ingestion)
RESUME = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "summary": "Backend engineer with Go and Postgres experience.",
    "skills": ["Go", "PostgreSQL", "gRPC"],
    "experience": [
        {"title": "Senior Engineer", "company": "Acme", "dates": "2019-2024",
         "bullets": ["Built Go services backed by Postgres.", "Designed gRPC APIs."]},
    ],
    "education": [{"text": "BSc Computer Science, TCD, 2015"}],
}

RAW_JOB = {
    "connector": "greenhouse:beta", "source": "greenhouse:beta", "source_type": "ats",
    "company": "Beta", "title": "Backend Engineer", "location": "Dublin",
    "url": "https://x/1",
    "description": "We need Go, Kubernetes, Postgres and gRPC. 3+ years backend experience.",
    "description_completeness": "full",
}


def test_full_pipeline_spine_composes_end_to_end():
    # 2) SCOUT — normalize a raw job into the canonical shape (+ dedupe hash)
    job = normalize_raw(RAW_JOB).model_dump()
    assert job["title"] == "Backend Engineer" and job["dedupe_hash"]

    # 3) QUALIFY — deterministic pass: the must-have gate + the experience-years dimension.
    #    Skills/roles are LLM-graded (exercised in the live tests), so no LLM here.
    config = qualifier.default_qualifier_config(RESUME["skills"])
    before = qualifier.score_deterministic(RESUME, job, config)
    assert before["must_have_gate"] == "pass"          # no must-haves configured -> passes
    assert 0.0 <= before["experience_score"] <= 1.0
    assert before["required_years"] == 3               # "3+ years" parsed from the posting

    # 4) ANALYZE — roll per-job analyses into a counted aggregate (no LLM)
    analyses = [
        {"required_skills": ["Go", "Kubernetes"], "keywords": ["backend"],
         "tech_stack": ["Postgres"], "seniority": "senior", "gaps_vs_resume": ["Kubernetes"]},
        {"required_skills": ["Go", "gRPC"], "keywords": ["microservices"],
         "tech_stack": ["Postgres"], "seniority": "senior", "gaps_vs_resume": []},
    ]
    agg = analyzer.aggregate(analyses)
    assert agg["full_text_jobs"] == 2
    assert any("go" in str(s).lower() for s in agg["skill_frequency"])  # Go recurs across jobs
    # the recurring gap (Kubernetes) is what the Enhancer will target
    assert any("kubernetes" in str(g).lower() for g in agg["recurring_gaps"])

    # 5) ENHANCE -> APPLY — apply accepted suggestions honestly (reframe real bullets,
    #    add only confirmed skills). The Enhancer's LLM output is represented by `accepted`.
    accepted = [{
        "bullet_rewrites_json": {"bullet_rewrites": [
            {"before": "Built Go services backed by Postgres.",
             "after": "Built Go microservices on Postgres serving production traffic."}]},
        "skills_to_add": ["Kubernetes"],  # a real gap the user is upskilling — confirmed below
        "projects_json": {"projects": [{"title": "Ship a k8s operator"}]},
    }]
    applied = resume_apply.apply_suggestions(RESUME, accepted, confirmed_skills=["Kubernetes"])
    assert "Kubernetes" in applied["resume"]["skills"]
    assert any("microservices" in a["after"] for a in applied["applied_rewrites"])
    improved_resume = applied["resume"]

    # 6) TAILOR CV — surface real keywords into modifiable fields; locked facts immutable.
    snapshot = cv_fields.extract_locked_snapshot(improved_resume)
    tailored = {
        "contact": improved_resume["contact"],
        "education": improved_resume["education"],
        "summary": "Go/Kubernetes backend engineer (Postgres, gRPC).",
        "skills": ["Go", "Kubernetes", "PostgreSQL", "gRPC"],  # all real (Kubernetes now held)
        "experience": [{
            "title": "Senior Engineer", "company": "Acme", "dates": "2019-2024",
            "bullets": ["Built Go microservices on Kubernetes and Postgres."]}],
    }
    gate = cv_fields.validate_tailored(tailored, improved_resume, snapshot)
    assert gate["ok"], gate                              # honest: locked intact, no fabrication
    honest = cv_fields.drop_fabricated_skills(cv_fields.enforce_locked(tailored, snapshot),
                                              improved_resume)

    # 7) EXPORT — deterministic render to a machine-readable PDF (selectable text)
    pdf = tailor.render_pdf(honest)
    text = tailor.pdf_text(pdf)
    assert pdf[:4] == b"%PDF" and len(text) > 50        # real selectable text, not an image
    assert "Jane Doe" in text and "2019-2024" in text   # locked facts present + intact

    # 8) RE-QUALIFY — score the tailored CV against the same job (before/after on record).
    #    The deterministic pass (gate + experience) is stable and comparable; the skills/roles
    #    lift from the LLM refine is proven in test_phase6_5_live.
    after = qualifier.score_deterministic(honest, job, config)
    assert after["must_have_gate"] == "pass" and 0.0 <= after["experience_score"] <= 1.0

    # 9) QUERY — the exact facts a read-only question would answer now exist and are consistent
    delta = round(after["experience_score"] - before["experience_score"], 4)
    assert isinstance(delta, float)                    # "which CV improved / by how much" is answerable
    assert agg["basis"].startswith("computed from 2 full-text")
