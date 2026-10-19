"""Projects as a typed section: structure, placement, bullet cap, and one-page fitting.

A project on a real CV is structurally a role — name, stack, dates, bullets — and was
previously stored as ``AdditionalSection``, a flat ``list[str]``. That threw the structure
away at parse time and made it unrecoverable at render time: the project's header line
became just another bullet and its dates sat inline instead of right-aligned.

These pin the four things that fix depends on: the typed shape survives to the page, it
renders between Experience and Education, no entry prints more than five bullets, and the
document shrinks to one page rather than spilling.
"""

from __future__ import annotations

from jobpilot_core import cv_fields, tailor

BASE = {
    "contact": {"name": "Jane Doe", "location": "Dublin, Ireland", "email": "jane@x.com"},
    "summary": "Engineer.",
    "skills": ["Python", "LangGraph"],
    "experience": [
        {"title": "Engineer", "company": "Acme", "location": "Dublin", "dates": "2021-2024",
         "bullets": [f"Did thing {i}." for i in range(8)]},
    ],
    "education": [{"text": "BSc Computer Science, UCD 2017 - 2021"}],
    "projects": [
        {"name": "Multi-Agent Platform", "tech": "Python, LangGraph, pgvector",
         "dates": "Feb. 2026 - Present",
         "bullets": [f"Built component {i}." for i in range(7)]},
        {"name": "Restaurant Recommender", "tech": "Qwen2.5, CLIP, ChromaDB",
         "dates": "Sep. 2025 - Feb. 2026",
         "bullets": ["Built an end-to-end multimodal recommender."]},
    ],
}


# --------------------------------------------------------------------------- #
# Structure survives to the page
# --------------------------------------------------------------------------- #
def test_the_typed_shape_reaches_the_renderer():
    projects = tailor._sections(BASE)["projects"]
    assert [p["name"] for p in projects] == ["Multi-Agent Platform", "Restaurant Recommender"]
    assert projects[0]["tech"] == "Python, LangGraph, pgvector"
    assert projects[0]["dates"] == "Feb. 2026 - Present"


def test_name_tech_and_dates_print_as_separate_pieces_not_one_bullet():
    """The visible symptom of the old flat shape: everything on one bulleted line."""
    text = tailor.pdf_text(tailor.render_pdf(BASE))
    assert "Multi-Agent Platform" in text
    assert "Python, LangGraph, pgvector" in text
    assert "Feb. 2026" in text
    # The header line must not have been swallowed into the tech string as one run.
    assert "Multi-Agent Platform | Python, LangGraph, pgvector Feb. 2026" not in text


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #
def test_projects_render_between_experience_and_education():
    text = tailor.pdf_text(tailor.render_pdf(BASE))
    exp, proj, edu = (text.index(h) for h in ("EXPERIENCE", "PROJECTS", "EDUCATION"))
    assert exp < proj < edu


def test_a_cv_without_projects_is_unchanged():
    """The section must be absent, not empty-with-a-header, when there is nothing to show."""
    no_projects = {k: v for k, v in BASE.items() if k != "projects"}
    text = tailor.pdf_text(tailor.render_pdf(no_projects))
    assert "PROJECTS" not in text


# --------------------------------------------------------------------------- #
# Bullet cap
# --------------------------------------------------------------------------- #
def test_no_entry_prints_more_than_five_bullets():
    s = tailor._sections(BASE)
    assert len(s["experience"][0]["bullets"]) == tailor.MAX_BULLETS == 5
    assert len(s["projects"][0]["bullets"]) == 5
    # A short entry is left alone rather than padded.
    assert len(s["projects"][1]["bullets"]) == 1


def test_the_cap_keeps_the_first_bullets_because_they_are_the_relevant_ones():
    """The rewriter orders bullets most-relevant-first for the target job, so truncating
    the tail keeps what matters. Dropping from the front would invert that."""
    kept = tailor._sections(BASE)["projects"][0]["bullets"]
    assert kept == [f"Built component {i}." for i in range(5)]


# --------------------------------------------------------------------------- #
# One-page fitting
# --------------------------------------------------------------------------- #
def test_a_cv_that_overflows_at_full_size_is_shrunk_onto_one_page():
    """Five roles of five bullets plus two projects spills at the template's own scale and
    fits once shrunk — the case the fitting loop exists for."""
    big = {
        **BASE,
        "experience": [
            {"title": f"Engineer {n}", "company": f"Company {n}", "location": "Dublin",
             "dates": "2020-2024",
             "bullets": [f"Delivered a substantial piece of work number {i} "
                         f"with measurable outcomes and detail." for i in range(5)]}
            for n in range(5)
        ],
    }
    assert tailor._render_pdf_at(big, 1.0)[1] > 1     # would have overflowed
    assert _page_count(tailor.render_pdf(big)) == 1   # but does not


def test_a_cv_that_already_fits_is_rendered_at_full_size():
    """The common case must not be silently shrunk: scale 1.0 is tried first and wins.

    Compared by rendered text and page count rather than bytes — reportlab stamps a
    creation timestamp into every PDF, so two renders of identical content never match
    byte-for-byte.
    """
    at_full, pages = tailor._render_pdf_at(BASE, 1.0)
    assert pages == 1
    assert tailor.pdf_text(tailor.render_pdf(BASE)) == tailor.pdf_text(at_full)


def test_shrinking_stops_at_the_floor_rather_than_becoming_unreadable():
    """Content that cannot fit at 80% runs to a second page instead of shrinking away."""
    enormous = {
        **BASE,
        "experience": [
            {"title": f"Engineer {n}", "company": f"Company {n}", "dates": "2020-2024",
             "bullets": [f"A long and detailed accomplishment line number {i} that wraps "
                         f"across more than one line of the page." for i in range(5)]}
            for n in range(24)
        ],
    }
    data = tailor.render_pdf(enormous)
    assert _page_count(data) > 1  # not fitted, but not mangled either
    assert "Engineer 23" in tailor.pdf_text(data)  # and nothing was dropped


def _page_count(data: bytes) -> int:
    from pypdf import PdfReader
    import io

    return len(PdfReader(io.BytesIO(data)).pages)


# --------------------------------------------------------------------------- #
# Legacy rows still render properly
# --------------------------------------------------------------------------- #
def test_a_legacy_flat_projects_section_is_recovered_into_the_typed_shape():
    """Drafts written before the typed field exist in the database. They must not regress
    to bullet soup just because they predate the schema."""
    legacy = {
        **{k: v for k, v in BASE.items() if k != "projects"},
        "additional_sections": [{"heading": "Projects", "items": [
            "Multi-Agent Platform | Python, LangGraph Feb. 2026 - Present",
            "Built a control plane.",
            "Routed data access through MCP services.",
        ]}],
    }
    projects = tailor._sections(legacy)["projects"]
    assert len(projects) == 1
    assert projects[0]["name"] == "Multi-Agent Platform"
    assert projects[0]["tech"] == "Python, LangGraph"
    assert projects[0]["dates"] == "Feb. 2026 - Present"
    assert projects[0]["bullets"] == ["Built a control plane.",
                                      "Routed data access through MCP services."]


def test_a_legacy_projects_section_does_not_also_print_as_a_trailing_header():
    """Recovered projects render in their proper place; the flat copy must not print again
    after Certifications, or the CV carries the section twice."""
    legacy = {
        **{k: v for k, v in BASE.items() if k != "projects"},
        "additional_sections": [{"heading": "Projects", "items": [
            "Multi-Agent Platform | Python Feb. 2026 - Present", "Built a control plane."]}],
    }
    text = tailor.pdf_text(tailor.render_pdf(legacy))
    assert text.count("PROJECTS") == 1


def test_a_non_projects_extra_section_still_renders_after_certifications():
    cv = {**BASE, "additional_sections": [
        {"heading": "Publications", "items": ["A paper, SIGMOD 2023."]}]}
    text = tailor.pdf_text(tailor.render_pdf(cv))
    assert "PUBLICATIONS" in text
    assert text.index("EDUCATION") < text.index("PUBLICATIONS")


# --------------------------------------------------------------------------- #
# A project's identity is locked, its bullets are not
# --------------------------------------------------------------------------- #
def test_project_name_tech_and_dates_are_re_imposed_from_the_snapshot():
    snapshot = cv_fields.extract_locked_snapshot(BASE)
    tampered = {
        **BASE,
        "projects": [
            {"name": "Multi-Agent Platform (Lead)", "tech": "Python, Kubernetes, Go",
             "dates": "Jan. 2024 - Present", "bullets": ["Reworded for the job."]},
            {"name": "Restaurant Recommender", "tech": "Qwen2.5, CLIP, ChromaDB",
             "dates": "Sep. 2025 - Feb. 2026", "bullets": ["Also reworded."]},
        ],
    }
    out = cv_fields.enforce_locked(tampered, snapshot)
    assert out["projects"][0]["name"] == "Multi-Agent Platform"
    assert out["projects"][0]["tech"] == "Python, LangGraph, pgvector"
    assert out["projects"][0]["dates"] == "Feb. 2026 - Present"
    # The one part the rewriter is allowed to change survives.
    assert out["projects"][0]["bullets"] == ["Reworded for the job."]


def test_a_skill_evidenced_only_by_a_typed_project_is_not_called_fabrication():
    draft = {**BASE, "skills": ["Python", "pgvector"]}
    assert cv_fields.validate_no_fabricated_skills(draft, BASE)["ok"]
