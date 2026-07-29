"""Tailor — lays the aligned CV out as a proper A4 Irish CV. Deterministic, NO LLM.

The alignment loop (:mod:`cv_loop`) decides *what the CV says*; the Tailor decides *how
it looks*, and it is the last stage before export. Given the loop's structured output it
produces **PDF + DOCX** honouring Irish conventions and ATS-parseability:

* **A4** (210×297mm), single column, standard fonts, generous margins;
* **selectable text** — never text-in-images, never layout tables (both break ATS
  parsing). ``pdf_text``/``docx_text`` read the text back out so a caller can prove it;
* **no photo, date of birth, gender, marital status, PPS number or nationality** — the
  Irish anti-discrimination norm. The renderer simply has no code path that emits them,
  so no upstream mistake can put them on the page;
* Irish section order — **Profile → Key Skills → Experience → Education →
  Certifications → any further headers → "References available on request."**;
* every header the candidate's real CV carried is rendered, in that order, so tailoring
  never silently drops a section (British/Irish spelling is the content's job).

PDF uses **reportlab** (pure-Python, no system libraries — identical output on Windows,
CI and the Linux image). WeasyPrint is the work-order's named tool but needs GTK/pango/
cairo, which aren't available on Windows/CI; reportlab meets every constraint above and
is verifiable in tests. DOCX uses **python-docx**.
"""

from __future__ import annotations

import io

REFERENCES_LINE = "References available on request."

# Irish CV section order. Anything the candidate had beyond these renders after
# Certifications, in the order the CV carried it.
PROFILE_HEADING = "Profile"
SKILLS_HEADING = "Key Skills"
EXPERIENCE_HEADING = "Experience"
EDUCATION_HEADING = "Education"
CERTIFICATIONS_HEADING = "Certifications"

# Never rendered, whatever an upstream stage puts in the payload (Irish norm).
SUPPRESSED_CONTACT_FIELDS = ("photo", "dob", "date_of_birth", "gender", "marital_status",
                             "nationality", "pps", "pps_number", "age")


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _sections(cv: dict) -> dict:
    """Normalise the loop's output into exactly what gets printed."""
    contact = cv.get("contact") or {}
    education = [
        _text(e.get("text")) if isinstance(e, dict) else _text(e)
        for e in (cv.get("education") or [])
    ]
    extra: list[dict] = []
    for sec in cv.get("additional_sections") or []:
        if not isinstance(sec, dict):
            continue
        heading = _text(sec.get("heading"))
        items = [_text(i) for i in (sec.get("items") or []) if _text(i)]
        if heading and items:
            extra.append({"heading": heading, "items": items})
    return {
        "contact": contact,
        "summary": _text(cv.get("summary")),
        "skills": [_text(s) for s in (cv.get("skills") or []) if _text(s)],
        "experience": [e for e in (cv.get("experience") or []) if isinstance(e, dict)],
        "education": [e for e in education if e],
        "certifications": [_text(c) for c in (cv.get("certifications") or []) if _text(c)],
        "additional_sections": extra,
    }


def _contact_line(contact: dict) -> str:
    """Email • phone • location • links. Discriminatory fields are never emitted."""
    bits = [contact.get("email"), contact.get("phone"), contact.get("location")]
    bits += list(contact.get("links") or [])
    return "  •  ".join(_text(b) for b in bits if _text(b))


def _role_heading(role: dict) -> str:
    return " — ".join(_text(x) for x in (role.get("title"), role.get("company")) if _text(x))


def _bullets(role: dict) -> list[str]:
    return [_text(b) for b in (role.get("bullets") or []) if _text(b)]


# --------------------------------------------------------------------------- #
# PDF (reportlab) — A4, single column, Helvetica, selectable text
# --------------------------------------------------------------------------- #
def render_pdf(cv: dict) -> bytes:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer,
    )

    def esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    s = _sections(cv)

    name_st = ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=18, leading=22, alignment=TA_LEFT)
    meta_st = ParagraphStyle("meta", fontName="Helvetica", fontSize=9.5, leading=13, textColor="#333333")
    h_st = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=12, leading=16, spaceBefore=10, spaceAfter=2)
    body_st = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14)
    role_st = ParagraphStyle("role", fontName="Helvetica-Bold", fontSize=10.5, leading=14, spaceBefore=6)
    dates_st = ParagraphStyle("dates", fontName="Helvetica-Oblique", fontSize=9, leading=12, textColor="#555555")

    story: list = []
    story.append(Paragraph(esc(_text(s["contact"].get("name")) or "Curriculum Vitae"), name_st))
    line = _contact_line(s["contact"])
    if line:
        story.append(Paragraph(esc(line), meta_st))
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=0.6, color="#999999"))

    def section(title: str) -> None:
        story.append(Paragraph(esc(title), h_st))

    def bullet_list(items: list[str]) -> None:
        story.append(ListFlowable(
            [ListItem(Paragraph(esc(i), body_st), leftIndent=10) for i in items],
            bulletType="bullet", start="•", leftIndent=12))

    if s["summary"]:
        section(PROFILE_HEADING)
        story.append(Paragraph(esc(s["summary"]), body_st))
    if s["skills"]:
        section(SKILLS_HEADING)
        story.append(Paragraph(esc("  •  ".join(s["skills"])), body_st))
    if s["experience"]:
        section(EXPERIENCE_HEADING)
        for role in s["experience"]:
            head = _role_heading(role)
            if head:
                story.append(Paragraph(esc(head), role_st))
            if _text(role.get("dates")):
                story.append(Paragraph(esc(_text(role["dates"])), dates_st))
            bullets = _bullets(role)
            if bullets:
                bullet_list(bullets)
    if s["education"]:
        section(EDUCATION_HEADING)
        for ed in s["education"]:
            story.append(Paragraph(esc(ed), body_st))
    if s["certifications"]:
        section(CERTIFICATIONS_HEADING)
        bullet_list(s["certifications"])
    for extra in s["additional_sections"]:
        section(extra["heading"])
        bullet_list(extra["items"])

    story.append(Spacer(1, 8))
    story.append(Paragraph(esc(REFERENCES_LINE), dates_st))

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm,
        leftMargin=18 * mm, rightMargin=18 * mm, title="CV",
    ).build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# DOCX (python-docx) — A4, single column, selectable text
# --------------------------------------------------------------------------- #
def render_docx(cv: dict) -> bytes:
    import docx
    from docx.shared import Mm, Pt

    s = _sections(cv)
    document = docx.Document()
    for section in document.sections:  # A4
        section.page_width, section.page_height = Mm(210), Mm(297)
        section.top_margin = section.bottom_margin = Mm(16)
        section.left_margin = section.right_margin = Mm(18)

    name = document.add_paragraph()
    run = name.add_run(_text(s["contact"].get("name")) or "Curriculum Vitae")
    run.bold = True
    run.font.size = Pt(18)
    line = _contact_line(s["contact"])
    if line:
        document.add_paragraph(line)

    def heading(title: str) -> None:
        p = document.add_paragraph()
        r = p.add_run(title)
        r.bold = True
        r.font.size = Pt(12)

    if s["summary"]:
        heading(PROFILE_HEADING)
        document.add_paragraph(s["summary"])
    if s["skills"]:
        heading(SKILLS_HEADING)
        document.add_paragraph("  •  ".join(s["skills"]))
    if s["experience"]:
        heading(EXPERIENCE_HEADING)
        for role in s["experience"]:
            head = _role_heading(role)
            if head:
                p = document.add_paragraph()
                p.add_run(head).bold = True
            if _text(role.get("dates")):
                document.add_paragraph(_text(role["dates"])).italic = True
            for b in _bullets(role):
                document.add_paragraph(b, style="List Bullet")
    if s["education"]:
        heading(EDUCATION_HEADING)
        for ed in s["education"]:
            document.add_paragraph(ed)
    if s["certifications"]:
        heading(CERTIFICATIONS_HEADING)
        for c in s["certifications"]:
            document.add_paragraph(c, style="List Bullet")
    for extra in s["additional_sections"]:
        heading(extra["heading"])
        for item in extra["items"]:
            document.add_paragraph(item, style="List Bullet")

    document.add_paragraph(REFERENCES_LINE)

    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Machine-readability checks — extract text back out of the rendered files
# --------------------------------------------------------------------------- #
def pdf_text(data: bytes) -> str:
    """Extract selectable text from a rendered PDF (proves it's not an image)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def docx_text(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs).strip()


def render_both(cv: dict) -> dict:
    """Render PDF + DOCX; return bytes + a machine-readability verdict for each."""
    pdf, docx_bytes = render_pdf(cv), render_docx(cv)
    ptext = pdf_text(pdf)
    return {
        "pdf": pdf,
        "docx": docx_bytes,
        "pdf_text_len": len(ptext),
        "pdf_machine_readable": len(ptext) >= 50,  # real extractable text, not an image
    }
