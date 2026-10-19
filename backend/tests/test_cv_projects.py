"""The Projects section is real evidence, and the tailor must treat it as such.

A CV's extra headed sections — Projects, Publications, Volunteering — live in
``additional_sections``. Three separate places used to behave as though they did not
exist, and the combination was invisible from the outside:

1. the résumé block handed to the models omitted them, so the rewriter could not reword
   what it had never been shown;
2. ``cv_fields._base_text`` — the haystack every fabrication check searches — omitted them
   too (and certifications), so a skill whose only evidence was a real project was deleted
   from the draft as a *fabrication*; and
3. ``additional_sections`` was optional in the rewriter's tool schema, so the model left
   the key out and ``carry_passthrough_sections`` restored the base copy verbatim — the
   section arrived on the exported CV looking untouched, because it was.

Together they made the honesty machinery run backwards: it was suppressing true claims.
These tests pin each layer independently, because any one of them regressing alone would
reproduce the whole symptom.
"""

from __future__ import annotations

from jobpilot_core import cv_fields, cv_rewriter
from jobpilot_core.analyzer import _resume_block

BASE_RESUME = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "summary": "Backend engineer.",
    "skills": ["Python", "PostgreSQL"],
    "experience": [
        {"title": "Engineer", "company": "Acme", "location": "Dublin", "dates": "2021-2024",
         "bullets": ["Built REST services in Python.", "Tuned PostgreSQL queries."]},
    ],
    "education": [{"text": "BSc Computer Science, UCD"}],
    "certifications": ["AWS Solutions Architect"],
    "additional_sections": [
        {"heading": "Projects", "items": [
            "Kubernetes deployment pipeline for a 12-service side project, with Helm and ArgoCD.",
            "Open-source Terraform module for zero-downtime Postgres failover.",
        ]},
        {"heading": "Publications", "items": ["A paper on query planning, SIGMOD 2023."]},
    ],
}


def _snapshot():
    return cv_fields.extract_locked_snapshot(BASE_RESUME)


# --------------------------------------------------------------------------- #
# Layer 1 — the models are shown the sections
# --------------------------------------------------------------------------- #
def test_the_analyzer_digest_names_the_extra_sections():
    """Without this the Analyzer reports gaps the CV has already closed, which then sends
    the Gap-closer off proposing projects the candidate has demonstrably already built."""
    block = _resume_block(BASE_RESUME)
    assert "Projects:" in block
    assert "Kubernetes" in block
    assert "SIGMOD" in block


def test_the_rewriter_sees_the_whole_cv_not_a_digest():
    """The rewriter's block is uncapped on purpose: it is the cached prefix, and an agent
    asked to reword a CV can only reword what it was shown."""
    block = cv_rewriter._full_resume_block(BASE_RESUME)
    assert "Terraform" in block and "SIGMOD" in block
    assert "AWS Solutions Architect" in block


def test_the_rewriter_block_keeps_every_bullet_of_every_role():
    """The old digest capped roles at ten and bullets at four, so a long role's later
    bullets were invisible — and a bullet the rewriter never saw is a bullet it drops."""
    wordy = {
        **BASE_RESUME,
        "experience": [
            {"title": "Engineer", "company": "Acme", "dates": "2021-2024",
             "bullets": [f"Did notable thing number {i}." for i in range(9)]},
        ],
    }
    block = cv_rewriter._full_resume_block(wordy)
    for i in range(9):
        assert f"number {i}." in block


# --------------------------------------------------------------------------- #
# Layer 2 — a project counts as evidence
# --------------------------------------------------------------------------- #
def test_a_skill_evidenced_only_by_a_project_is_not_called_fabrication():
    draft = {**BASE_RESUME, "skills": ["Python", "Kubernetes", "Terraform"]}
    verdict = cv_fields.validate_no_fabricated_skills(draft, BASE_RESUME)
    assert verdict["ok"], verdict
    kept = cv_fields.drop_fabricated_skills(draft, BASE_RESUME)
    assert "Kubernetes" in kept["skills"] and "Terraform" in kept["skills"]


def test_a_citation_quoting_a_project_verifies():
    draft = {
        **BASE_RESUME,
        "skills": ["Python", "Helm"],
        "skill_evidence": [
            {"skill": "Helm", "basis": "explicit",
             "evidence": "Kubernetes deployment pipeline for a 12-service side project, with Helm and ArgoCD."},
        ],
    }
    result = cv_fields.validate_skill_evidence(draft, BASE_RESUME)
    assert result["ok"], result["rejected"]


def test_a_skill_evidenced_only_by_a_certification_is_not_called_fabrication():
    """Same omission, same consequence — certifications were missing from the haystack too."""
    draft = {**BASE_RESUME, "skills": ["Python", "AWS Solutions Architect"]}
    assert cv_fields.validate_no_fabricated_skills(draft, BASE_RESUME)["ok"]


def test_a_genuinely_invented_skill_is_still_caught():
    """The widened haystack must not become a hole: a skill that appears nowhere on the
    real CV is still fabrication."""
    draft = {**BASE_RESUME, "skills": ["Python", "COBOL"]}
    verdict = cv_fields.validate_no_fabricated_skills(draft, BASE_RESUME)
    assert not verdict["ok"] and verdict["fabricated"] == ["COBOL"]


# --------------------------------------------------------------------------- #
# Layer 3 — the rewriter must address the sections, and cannot lose them
# --------------------------------------------------------------------------- #
def test_additional_sections_are_required_output():
    """Optional meant omitted, and omitted meant the base copy came back untouched."""
    assert "additional_sections" in cv_rewriter.CV_SCHEMA["required"]


def test_a_tailored_section_survives_finalize():
    """The point of the whole fix: reworded project text must reach the exported CV."""
    tailored = {
        **BASE_RESUME,
        "additional_sections": [
            {"heading": "Projects", "items": [
                "Built and operated a Kubernetes delivery pipeline across 12 services (Helm, ArgoCD).",
                "Authored an open-source Terraform module delivering zero-downtime Postgres failover.",
            ]},
            {"heading": "Publications", "items": ["Query-planning research published at SIGMOD 2023."]},
        ],
    }
    out = cv_fields.finalize(tailored, BASE_RESUME, _snapshot())
    projects = out["additional_sections"][0]
    assert projects["items"][0].startswith("Built and operated")
    assert out["additional_sections"] != BASE_RESUME["additional_sections"]


def test_an_omitted_section_still_falls_back_to_the_real_one():
    model_output = {k: v for k, v in BASE_RESUME.items() if k != "additional_sections"}
    out = cv_fields.finalize(model_output, BASE_RESUME, _snapshot())
    assert out["additional_sections"] == BASE_RESUME["additional_sections"]


def test_a_section_the_rewriter_dropped_is_restored_individually():
    """The failure mode that only became possible once the model started filling the field
    in: it returns Projects and quietly forgets Publications."""
    partial = {
        **BASE_RESUME,
        "additional_sections": [{"heading": "Projects", "items": ["Reworded project."]}],
    }
    out = cv_fields.finalize(partial, BASE_RESUME, _snapshot())
    headings = [s["heading"] for s in out["additional_sections"]]
    assert headings == ["Projects", "Publications"]
    assert out["additional_sections"][1]["items"] == ["A paper on query planning, SIGMOD 2023."]


def test_a_section_emptied_of_items_falls_back_rather_than_printing_a_bare_header():
    hollow = {
        **BASE_RESUME,
        "additional_sections": [
            {"heading": "Projects", "items": []},
            {"heading": "Publications", "items": ["Query-planning research, SIGMOD 2023."]},
        ],
    }
    out = cv_fields.finalize(hollow, BASE_RESUME, _snapshot())
    assert out["additional_sections"][0]["items"] == BASE_RESUME["additional_sections"][0]["items"]


def test_a_rewriter_invented_heading_is_not_carried_through():
    """Sections come from the real CV. A new header is a new claim about the candidate."""
    inflated = {
        **BASE_RESUME,
        "additional_sections": [
            {"heading": "Projects", "items": ["Reworded project."]},
            {"heading": "Publications", "items": ["Reworded paper."]},
            {"heading": "Patents", "items": ["US-1234567, distributed consensus."]},
        ],
    }
    out = cv_fields.finalize(inflated, BASE_RESUME, _snapshot())
    assert [s["heading"] for s in out["additional_sections"]] == ["Projects", "Publications"]


def test_recased_headings_are_matched_not_duplicated():
    recased = {
        **BASE_RESUME,
        "additional_sections": [
            {"heading": "PROJECTS", "items": ["Reworded project."]},
            {"heading": "Publications", "items": ["Reworded paper."]},
        ],
    }
    out = cv_fields.finalize(recased, BASE_RESUME, _snapshot())
    assert len(out["additional_sections"]) == 2
    assert out["additional_sections"][0]["items"] == ["Reworded project."]
