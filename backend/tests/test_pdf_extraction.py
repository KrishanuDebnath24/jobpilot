"""PDF word spacing — the silent failure that made résumé text unmatchable.

pdfplumber infers word boundaries from the horizontal gap between glyphs, and its default
``x_tolerance`` of 3 points is wider than the inter-word gap in many real CV templates.
When that happens every word on the page is welded to its neighbour, and nothing errors:
the LLM parser recovers enough structure from the soup to produce a plausible résumé, so
the damage only shows up much later, in ``cv_fields._evidence_supported``, which matches
skill citations on *content words* and can match nothing at all against text with no word
boundaries. The evidence gate then deletes true skills for want of a space.

The fixtures here are generated with reportlab at a chosen inter-word gap, so the failure
is reproduced exactly and deterministically without shipping anyone's real CV.
"""

from __future__ import annotations

import io

import pytest
from jobpilot_core import cv_fields, resume_parse

pdfplumber = pytest.importorskip("pdfplumber")

SENTENCE = (
    "Built a LangGraph control plane with Postgres checkpointing sequencing eight "
    "cooperating agents over Celery and Redis workers so a crash mid run resumes rather "
    "than restarts and human in the loop gates pause consequential steps Routed all data "
    "access through five purpose scoped services with strict boundaries"
)


def _pdf(gap_pt: float, text: str = SENTENCE) -> bytes:
    """A one-page PDF whose words sit ``gap_pt`` apart — the single variable that decides
    whether the default extraction welds them."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 9)
    x, y = 40, 780
    for word in text.split():
        width = pdfmetrics.stringWidth(word, "Helvetica", 9)
        if x + width > 550:
            x, y = 40, y - 12
        c.drawString(x, y, word)
        x += width + gap_pt
    c.showPage()
    c.save()
    return buf.getvalue()


def _default_extract(data: bytes) -> str:
    """What the old code did: one pass at pdfplumber's default tolerance."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages).strip()


# --------------------------------------------------------------------------- #
# The bug
# --------------------------------------------------------------------------- #
def test_the_default_extraction_really_does_weld_tight_tracking():
    """Guards the premise. If pdfplumber ever fixes this, the rest of the file is moot
    and should be revisited rather than left silently passing for the wrong reason."""
    welded = _default_extract(_pdf(gap_pt=2.0))
    assert "Builta" in welded.replace(" ", "") or resume_parse._looks_welded(welded)


def test_a_tight_tracking_pdf_now_extracts_as_words():
    text = resume_parse._extract_pdf(_pdf(gap_pt=2.0))
    assert "Built a LangGraph control plane" in text
    assert not resume_parse._looks_welded(text)
    # The real measure: nearly every word of the source survives as its own token.
    assert len(text.split()) >= len(SENTENCE.split()) - 2


def test_a_normally_spaced_pdf_is_left_exactly_as_it_was():
    """The common case must not pay for the rare one, in cost or in behaviour: a healthy
    document returns from the first pass without the sweep running at all."""
    data = _pdf(gap_pt=4.0)
    assert resume_parse._extract_pdf(data) == _default_extract(data)


# --------------------------------------------------------------------------- #
# The decision rule
# --------------------------------------------------------------------------- #
def test_word_score_is_two_sided():
    """Why the sweep can pick a winner at all. Welding pushes tokens above the word band,
    shattering pushes them below, so the correct extraction scores highest either way."""
    clean = SENTENCE
    welded = SENTENCE.replace(" ", "")
    shattered = " ".join(SENTENCE.replace(" ", ""))  # every character its own token
    assert resume_parse._word_score(clean) > resume_parse._word_score(welded)
    assert resume_parse._word_score(clean) > resume_parse._word_score(shattered)


def test_looks_welded_ignores_documents_too_short_to_judge():
    """A mostly-graphical CV can legitimately yield a handful of tokens; that is not
    evidence of welding, and treating it as such would trigger a pointless sweep."""
    assert not resume_parse._looks_welded("SomeVeryLongUnbrokenTokenIndeedYesReally")


def test_looks_welded_tolerates_a_few_long_urls():
    """Healthy CVs carry links, which are legitimately long. The threshold is a ratio, and
    a real extraction of a real CV scored 3 long tokens out of 827."""
    text = " ".join(["word"] * 200 + [
        "https://linkedin.com/in/someone-with-a-very-long-profile-slug",
        "https://github.com/someone/a-repository-with-a-long-name",
    ])
    assert not resume_parse._looks_welded(text)


# --------------------------------------------------------------------------- #
# Why it mattered
# --------------------------------------------------------------------------- #
def test_welded_text_makes_multi_word_skills_look_fabricated():
    """The consequence, end to end — and it is narrower than it first appears, which is
    worth pinning precisely so nobody "fixes" the wrong thing later.

    Grounding checks ask whether a normalised skill appears in the résumé text, as a
    substring. Welding leaves *single-word* skills perfectly findable — "langgraph" is
    still inside "builtalanggraphcontrolplane" — so they survive by luck. A skill of two
    or more words does not: the haystack has no spaces left for it to match against, so
    it is judged fabricated and deleted from the draft. Certifications and framework
    names are overwhelmingly multi-word, which is exactly the wrong population to lose.
    """
    text = "Built a LangGraph control plane using Machine Learning pipelines end to end"
    welded = {"experience": [{"bullets": [text.replace(" ", "")]}]}
    clean = {"experience": [{"bullets": [text]}]}

    single = {"skills": ["LangGraph"]}
    multi = {"skills": ["Machine Learning"]}

    # Single-word: survives welding, so the bug was invisible for these.
    assert cv_fields.validate_no_fabricated_skills(single, welded)["ok"]
    # Multi-word: deleted against welded text, kept against properly spaced text.
    assert not cv_fields.validate_no_fabricated_skills(multi, welded)["ok"]
    assert cv_fields.validate_no_fabricated_skills(multi, clean)["ok"]


def test_extraction_restores_multi_word_grounding_for_a_tight_pdf():
    """And the fix closes it for real PDF input, not just for hand-written strings."""
    resume = {"experience": [{"bullets": [resume_parse._extract_pdf(_pdf(gap_pt=2.0))]}]}
    draft = {"skills": ["Control Plane", "Redis Workers"]}
    assert cv_fields.validate_no_fabricated_skills(draft, resume)["ok"]
