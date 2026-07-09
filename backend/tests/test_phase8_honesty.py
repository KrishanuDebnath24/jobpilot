"""Phase 8e — end-to-end no-fabrication / honesty audit (deterministic).

The honesty guarantees now span three surfaces, each enforced **in code** (not merely
prompted). This adversarially probes all three and confirms fabrication is impossible by
construction, so a re-qualification score gain can only come from re-presenting REAL
content:

  * **apply-to-resume** (Phase 6): a fabricated bullet is dropped; a skill not confirmed
    is withheld; projects are never inserted.
  * **CV tailoring locked fields** (Phase 6.5): a tampered locked field (date/title/role
    count) is caught by the validator and re-imposed; a fabricated skill is caught + dropped.
  * **legitimacy of score gains**: after the code guards run, the draft's locked facts equal
    the base and its skills are all grounded — so any gain is real presentation, not gaming.

The live adversarial elicitation (the Critic catching a planted fabrication + tampered
date; the Enhancer refusing to fabricate) is covered by ``test_phase6_5_live.py`` /
``test_phase5*``; here are the deterministic code-level guarantees.
"""

from __future__ import annotations

from jobpilot_core import cv_fields, resume_apply

BASE_RESUME = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "summary": "Backend engineer with Go and Kubernetes experience.",
    "skills": ["Go", "Kubernetes", "PostgreSQL"],
    "experience": [
        {"title": "Senior Engineer", "company": "Acme", "dates": "2020-2024",
         "bullets": ["Built Go microservices on Kubernetes.", "Ran Postgres at scale."]},
    ],
    "education": [{"text": "BSc Computer Science, TCD, 2016"}],
}


# --------------------------------------------------------------------------- #
# Surface 1 — apply-to-resume drops fabrication, withholds unconfirmed skills
# --------------------------------------------------------------------------- #
def test_apply_drops_fabricated_bullet_and_withholds_unconfirmed_skill():
    accepted = [{
        "bullet_rewrites_json": {"bullet_rewrites": [
            # genuine reframe — its `before` matches a real bullet → applied
            {"before": "Built Go microservices on Kubernetes.",
             "after": "Architected Go microservices on Kubernetes serving 1M req/s."},
            # fabrication — no matching bullet in the resume → dropped
            {"before": "Led a team of 50 engineers across 4 countries.",
             "after": "Led a team of 50 engineers across 4 countries to deliver X."},
        ]},
        "skills_to_add": ["Rust", "Go"],  # Rust unconfirmed → withheld; Go already present
        "projects_json": {"projects": [{"title": "Build a Rust CLI"}]},  # never inserted
    }]
    out = resume_apply.apply_suggestions(BASE_RESUME, accepted, confirmed_skills=[])

    applied = [a["after"] for a in out["applied_rewrites"]]
    assert any("Architected Go microservices" in a for a in applied)
    assert not any("50 engineers" in a for a in applied)          # fabricated bullet dropped
    assert "Rust" not in out["resume"].get("skills", [])          # unconfirmed skill withheld
    assert "Rust" in out["withheld_skills"]
    assert out["project_guidance"]                                # surfaced, not inserted
    # the ORIGINAL resume is never mutated
    assert BASE_RESUME["experience"][0]["bullets"][0] == "Built Go microservices on Kubernetes."


def test_apply_adds_a_skill_only_when_confirmed():
    accepted = [{"skills_to_add": ["Rust"], "bullet_rewrites_json": {}, "projects_json": {}}]
    out = resume_apply.apply_suggestions(BASE_RESUME, accepted, confirmed_skills=["Rust"])
    assert "Rust" in out["resume"]["skills"] and "Rust" in out["added_skills"]


# --------------------------------------------------------------------------- #
# Surface 2 — CV tailoring: locked fields are immutable, skills can't be fabricated
# --------------------------------------------------------------------------- #
def _snapshot():
    return cv_fields.extract_locked_snapshot(BASE_RESUME)


def _legit_tailored():
    """A legitimately tailored draft: same locked facts, reworded summary/bullets,
    reordered real skills — nothing invented."""
    return {
        "contact": BASE_RESUME["contact"],
        "education": BASE_RESUME["education"],
        "summary": "Kubernetes-focused backend engineer (Go, Postgres).",
        "skills": ["Kubernetes", "Go", "PostgreSQL"],  # reordered, all real
        "experience": [{
            "title": "Senior Engineer", "company": "Acme", "dates": "2020-2024",
            "bullets": ["Designed Go microservices on Kubernetes.", "Operated Postgres at scale."],
        }],
    }


def test_legitimate_tailoring_passes_the_gate():
    v = cv_fields.validate_tailored(_legit_tailored(), BASE_RESUME, _snapshot())
    assert v["ok"] and not v["violations"] and not v["fabricated"]


def test_tampered_locked_date_is_caught_and_re_imposed():
    snap = _snapshot()
    tampered = _legit_tailored()
    tampered["experience"][0]["dates"] = "2016-2024"   # inflated tenure — a locked-field lie

    v = cv_fields.validate_locked(tampered, snap)
    assert not v["ok"] and any("dates" in msg for msg in v["violations"])
    # enforce re-imposes the true date verbatim (defense in depth), keeps the reworded bullets
    fixed = cv_fields.enforce_locked(tampered, snap)
    assert fixed["experience"][0]["dates"] == "2020-2024"
    assert "Designed Go microservices on Kubernetes." in fixed["experience"][0]["bullets"]
    assert cv_fields.validate_locked(fixed, snap)["ok"]


def test_tampered_title_and_role_count_are_caught():
    snap = _snapshot()
    lie = _legit_tailored()
    lie["experience"][0]["title"] = "VP of Engineering"     # promotion that never happened
    assert not cv_fields.validate_locked(lie, snap)["ok"]

    added_role = _legit_tailored()
    added_role["experience"].append({"title": "CTO", "company": "Startup", "dates": "2024-", "bullets": []})
    v = cv_fields.validate_locked(added_role, snap)
    assert not v["ok"] and any("role count" in m for m in v["violations"])


def test_fabricated_skill_is_caught_and_dropped():
    draft = _legit_tailored()
    draft["skills"] = draft["skills"] + ["Rust", "Kubernetes"]  # Rust appears nowhere real
    v = cv_fields.validate_no_fabricated_skills(draft, BASE_RESUME)
    assert not v["ok"] and v["fabricated"] == ["Rust"]
    cleaned = cv_fields.drop_fabricated_skills(draft, BASE_RESUME)
    assert "Rust" not in cleaned["skills"] and "Kubernetes" in cleaned["skills"]


# --------------------------------------------------------------------------- #
# Surface 3 — a score gain is structurally attributable to real content
# --------------------------------------------------------------------------- #
def test_after_the_guards_a_draft_is_honest_by_construction():
    """A model draft that both tampered a date AND invented a skill, once passed through
    enforce_locked + drop_fabricated_skills, is byte-identical on locked facts and carries
    only grounded skills — so any re-qualification gain is real presentation, not gaming."""
    snap = _snapshot()
    dishonest = _legit_tailored()
    dishonest["experience"][0]["dates"] = "2010-2024"      # tampered
    dishonest["skills"] = dishonest["skills"] + ["Rust"]   # fabricated

    honest = cv_fields.drop_fabricated_skills(cv_fields.enforce_locked(dishonest, snap), BASE_RESUME)
    gate = cv_fields.validate_tailored(honest, BASE_RESUME, snap)
    assert gate["ok"], gate
    assert honest["experience"][0]["dates"] == "2020-2024"  # real date restored
    assert "Rust" not in honest["skills"]                   # invented skill gone
