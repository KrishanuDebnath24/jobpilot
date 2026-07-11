"""CV-attach unit tests (no DB, no LLM key).

Covers the CV-aware qualifier's effective-résumé selection and the resume-file media-type
helper. The full Analyze→Tailor→Export chain and the streaming endpoint are exercised in
the DB/live suites and manual E2E.
"""

from __future__ import annotations

from app.main import _resume_media_type
from jobpilot_core.qualifier import select_effective_resume

PARENT = {"skills": ["python"], "summary": "base", "experience": [{"title": "Engineer"}]}
TAILORED = {"skills": ["python", "fastapi"], "summary": "tailored", "experience": [{"title": "Engineer"}]}


def _draft(**over) -> dict:
    d = {"id": "draft-1", "pdf_ref": "resumes/u/cv/draft-1.pdf",
         "base_resume_version": "v-current", "tailored_content_json": TAILORED}
    d.update(over)
    return d


def test_current_exported_draft_wins():
    resume, source, version = select_effective_resume(PARENT, "v-current", _draft())
    assert resume is TAILORED
    assert source == "draft-1" and version == "draft-1"


def test_stale_draft_falls_back_to_parent():
    # Built from a previous résumé (e.g. after a new upload) → not the attached CV.
    resume, source, version = select_effective_resume(PARENT, "v-current", _draft(base_resume_version="v-old"))
    assert resume is PARENT
    assert source == "parent" and version == "v-current"


def test_unexported_draft_falls_back_to_parent():
    resume, source, _ = select_effective_resume(PARENT, "v-current", _draft(pdf_ref=None))
    assert resume is PARENT and source == "parent"


def test_empty_tailored_content_falls_back():
    resume, source, _ = select_effective_resume(PARENT, "v-current", _draft(tailored_content_json={}))
    assert resume is PARENT and source == "parent"


def test_no_draft_uses_parent():
    resume, source, version = select_effective_resume(PARENT, "v-current", None)
    assert resume is PARENT and source == "parent" and version == "v-current"


def test_resume_media_type_by_extension():
    assert _resume_media_type("resumes/u/abc.pdf") == ("application/pdf", "pdf")
    media, ext = _resume_media_type("resumes/u/abc.docx")
    assert ext == "docx" and media.endswith("wordprocessingml.document")
    # No extension → defaults to pdf; an unknown extension → octet-stream.
    assert _resume_media_type("resumes/u/abc") == ("application/pdf", "pdf")
    assert _resume_media_type("resumes/u/abc.xyz") == ("application/octet-stream", "xyz")
