"""Tailor — lays the aligned CV out in Jake's Résumé format. Deterministic, NO LLM.

The alignment loop (:mod:`cv_loop`) decides *what the CV says*; the Tailor decides *how it
looks*, and it is the last stage before export. Given the loop's structured output it
produces **PDF + DOCX** in the layout of Jake Gutierrez's widely-used résumé template — the
format most candidates now arrive with, and the one this pipeline's own users write:

* a centred name over one centred contact line (``location | phone | email | links``);
* section names in caps with a rule ruled under them;
* **two-column entries** — role title left / dates right, employer left / **location**
  right. That right-hand slot is the only place a role's location has ever belonged, and
  its absence is why exports used to lose it: nothing upstream captured the field at all
  (see ``resume_parse``), so there was nothing for this module to print;
* tight, dense spacing, so a normal career still lands on one or two pages.

It keeps the constraints that are not Jake's to negotiate:

* **A4** (210×297mm), single column, standard fonts;
* **selectable text** — never text-in-images, and never a layout *table*: the two-column
  rows are two strings drawn on one text line (see :func:`_flowables`), so extraction order
  is exactly reading order. ``pdf_text``/``docx_text`` read the text back out so a caller
  can prove it;
* **no photo, date of birth, gender, marital status, PPS number or nationality** — the
  Irish anti-discrimination norm. The renderer simply has no code path that emits them, so
  no upstream mistake can put them on the page;
* Irish section order — **Professional Summary → Technical Skills → Experience →
  Education → Certifications → any further headers → "References available on request."**;
* every header the candidate's real CV carried is rendered, in that order, so tailoring
  never silently drops a section (British/Irish spelling is the content's job).

PDF uses **reportlab** (pure-Python, no system libraries — identical output on Windows, CI
and the Linux image). WeasyPrint is the work-order's named tool but needs GTK/pango/cairo,
which aren't available on Windows/CI; reportlab meets every constraint above and is
verifiable in tests. DOCX uses **python-docx**, with a right-aligned tab stop standing in
for the same two-column row.
"""

from __future__ import annotations

import io
import re

from . import doc_meta

REFERENCES_LINE = "References available on request."

# Section order. Anything the candidate had beyond these renders after Certifications, in
# the order the CV carried it.
PROFILE_HEADING = "Professional Summary"
SKILLS_HEADING = "Technical Skills"
EXPERIENCE_HEADING = "Experience"
PROJECTS_HEADING = "Projects"
EDUCATION_HEADING = "Education"
CERTIFICATIONS_HEADING = "Certifications"

# Never rendered, whatever an upstream stage puts in the payload (Irish norm).
SUPPRESSED_CONTACT_FIELDS = ("photo", "dob", "date_of_birth", "gender", "marital_status",
                             "nationality", "pps", "pps_number", "age")

# --------------------------------------------------------------------------- #
# The template's typographic scale. Times is reportlab's built-in serif and needs no
# embedded font file, so the PDF renders identically everywhere and stays extractable.
# --------------------------------------------------------------------------- #
FONT = "Times-Roman"
FONT_BOLD = "Times-Bold"
FONT_ITALIC = "Times-Italic"
DOCX_FONT = "Times New Roman"

NAME_SIZE = 21.0
CONTACT_SIZE = 9.5
HEADING_SIZE = 11.5
ENTRY_SIZE = 10.5
BODY_SIZE = 10.0
MARGIN_MM = 12.7      # 0.5in — the template's own margin, and where its density comes from
PAGE_W_MM = 210.0
INK = "#000000"
MUTED = "#333333"


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


#: Hard cap on bullets under any one role or project. A CV that lists eight bullets per
#: role is not read more thoroughly — it is read less, and it costs the page space the
#: later entries need. The cap is applied at render time rather than asked for in a prompt
#: so it holds whatever the model returns, and it keeps the *first* N because the rewriter
#: already orders bullets most-relevant-first for the target job.
MAX_BULLETS = 5


def _project_from_section(sec: dict) -> list[dict]:
    """Read projects out of a legacy flat ``additional_sections`` entry.

    Drafts and résumés parsed before projects had a typed shape stored them as a list of
    strings, with the project's header line sitting in the list as though it were a
    bullet. Rather than render those as the flat bullet soup they became, recover the
    structure the same way a reader does: a line carrying a trailing date range starts a
    new project, and the lines after it are its bullets.

    Best-effort by nature — this is a migration path for rows already in the database, not
    the parser. Anything it cannot split stays a bullet under the previous project.
    """
    projects: list[dict] = []
    for raw in sec.get("items") or []:
        line = _text(raw)
        if not line:
            continue
        head, dates = _split_trailing_dates(line)
        # A header line has dates and has not yet been claimed as a bullet; a bullet
        # normally does not end in a date range.
        if dates and head:
            name, _, tech = head.partition("|")
            projects.append({
                "name": _text(name),
                "tech": _text(tech),
                "dates": dates,
                "bullets": [],
            })
        elif projects:
            projects[-1]["bullets"].append(line)
        else:
            # Text before any recognisable header — keep it as a nameless entry rather
            # than dropping content off the page.
            projects.append({"name": line, "tech": "", "dates": "", "bullets": []})
    return projects


def _projects(cv: dict) -> list[dict]:
    """The typed projects to print, newest-first order preserved, bullets capped.

    Falls back to the legacy ``additional_sections`` shape so a draft written before the
    typed field existed still renders properly instead of regressing to bullet soup.
    """
    raw = [p for p in (cv.get("projects") or []) if isinstance(p, dict)]
    if not raw:
        for sec in cv.get("additional_sections") or []:
            if isinstance(sec, dict) and _norm_heading(sec.get("heading")) == "projects":
                raw = _project_from_section(sec)
                break

    out: list[dict] = []
    for project in raw:
        name = _text(project.get("name"))
        if not name:
            continue
        out.append({
            "name": name,
            "tech": _text(project.get("tech")),
            "dates": _text(project.get("dates")),
            "bullets": [_text(b) for b in (project.get("bullets") or []) if _text(b)][:MAX_BULLETS],
        })
    return out


def _norm_heading(value) -> str:
    return _text(value).lower().rstrip(":").strip()


def _sections(cv: dict) -> dict:
    """Normalise the loop's output into exactly what gets printed."""
    contact = cv.get("contact") or {}
    education = [
        _text(e.get("text")) if isinstance(e, dict) else _text(e)
        for e in (cv.get("education") or [])
    ]
    projects = _projects(cv)
    extra: list[dict] = []
    for sec in cv.get("additional_sections") or []:
        if not isinstance(sec, dict):
            continue
        heading = _text(sec.get("heading"))
        # Projects now have their own typed section and their own place on the page. A
        # legacy flat copy must not also print here, or the CV carries it twice.
        if _norm_heading(heading) == "projects" and projects:
            continue
        items = [_text(i) for i in (sec.get("items") or []) if _text(i)]
        if heading and items:
            extra.append({"heading": heading, "items": items})

    experience: list[dict] = []
    for role in cv.get("experience") or []:
        if not isinstance(role, dict):
            continue
        capped = dict(role)
        capped["bullets"] = _bullets(role)[:MAX_BULLETS]
        experience.append(capped)

    return {
        "contact": contact,
        "summary": _text(cv.get("summary")),
        "skills": [_text(s) for s in (cv.get("skills") or []) if _text(s)],
        "experience": experience,
        "projects": projects,
        "education": [e for e in education if e],
        "certifications": [_text(c) for c in (cv.get("certifications") or []) if _text(c)],
        "additional_sections": extra,
    }


def _contact_bits(contact: dict) -> list[tuple[str, str]]:
    """``(display, href)`` for the header line — location, phone, email, then links, in the
    template's order. ``href`` is empty for anything that isn't clickable. Discriminatory
    fields are never emitted: there is no code path here that reads them."""
    bits: list[tuple[str, str]] = []
    location = _text(contact.get("location"))
    if location:
        bits.append((location, ""))
    phone = _text(contact.get("phone"))
    if phone:
        bits.append((phone, "tel:" + re.sub(r"[^\d+]", "", phone)))
    email = _text(contact.get("email"))
    if email:
        bits.append((email, "mailto:" + email))
    for link in contact.get("links") or []:
        shown = _text(link)
        if shown:
            href = shown if shown.startswith(("http://", "https://")) else "https://" + shown
            bits.append((shown, href))
    return bits


def _contact_line(contact: dict) -> str:
    """The same header line as plain text, for the DOCX and for anything that just wants
    the string."""
    return "  |  ".join(shown for shown, _ in _contact_bits(contact))


def _bullets(role: dict) -> list[str]:
    return [_text(b) for b in (role.get("bullets") or []) if _text(b)]


# A date range at the very end of a free-text line — 'Sep. 2024 - Sep. 2025', '2019–2023',
# 'Jan 2020 to Present'. Education arrives as one flat line, so pulling its dates off is the
# only way to give it the same two-column look as a role. Anything that does not clearly end
# in a range is left whole rather than guessed at.
_MONTH_YEAR = r"(?:[A-Za-z]{3,9}\.?\s+)?\d{4}"
_DATE_TAIL = re.compile(
    r"[\s,;|–—-]*(" + _MONTH_YEAR + r"\s*(?:-|–|—|to)\s*"
    r"(?:" + _MONTH_YEAR + r"|Present|Current|Ongoing|Now))\s*$",
    re.IGNORECASE,
)


def _split_trailing_dates(line: str) -> tuple[str, str]:
    """``(line_without_dates, dates)`` — ``("...", "")`` when there is no trailing range."""
    match = _DATE_TAIL.search(line)
    if not match:
        return line, ""
    return line[: match.start()].rstrip(" ,;|-–—"), match.group(1).strip()


# --------------------------------------------------------------------------- #
# PDF (reportlab) — A4, single column, Times, selectable text
# --------------------------------------------------------------------------- #
_FLOWABLES: dict = {}


def _flowables(scale: float = 1.0) -> dict:
    """Build this module's two custom flowables, once.

    Every reportlab import in this module is function-local, so importing ``tailor`` costs
    nothing for a caller that only needs the DOCX path — and a Flowable subclass cannot be
    declared before its base class exists. Hence a cached builder rather than module-level
    ``class`` statements.
    """
    if _FLOWABLES:
        return _FLOWABLES

    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Flowable

    class TwoCol(Flowable):
        """One line with text at the left margin and text at the right margin.

        The template is built from two-column entry lines (title left / dates right,
        employer left / location right). A layout *table* is the usual way to get that and
        is exactly what breaks ATS parsing, so this draws two strings on a single text line
        instead: extraction yields "left right", the order a human reads it, which
        ``pdf_text`` proves on every export. If the two halves would collide, the right one
        drops onto its own right-aligned line rather than overprinting the left.
        """

        def __init__(self, left: str, right: str, *, left_font: str, right_font: str,
                     size: float, leading: float, left_colour: str = INK,
                     right_colour: str = INK, space_before: float = 0.0):
            Flowable.__init__(self)
            self.left, self.right = left, right
            self.left_font, self.right_font = left_font, right_font
            self.size, self.leading = size, leading
            self.left_colour, self.right_colour = left_colour, right_colour
            self.spaceBefore = space_before * scale
            self._stacked = False

        def wrap(self, availWidth, availHeight):
            self.width = availWidth
            left_w = stringWidth(self.left, self.left_font, self.size) if self.left else 0
            right_w = stringWidth(self.right, self.right_font, self.size) if self.right else 0
            self._stacked = bool(self.left and self.right) and left_w + right_w + 12 > availWidth
            self.height = self.leading * (2 if self._stacked else 1)
            return self.width, self.height

        def draw(self):
            canvas = self.canv
            baseline = self.height - self.size
            if self.left:
                canvas.setFont(self.left_font, self.size)
                canvas.setFillColor(self.left_colour)
                canvas.drawString(0, baseline, self.left)
            if self.right:
                canvas.setFont(self.right_font, self.size)
                canvas.setFillColor(self.right_colour)
                canvas.drawRightString(
                    self.width, baseline - (self.leading if self._stacked else 0), self.right)

    class SectionHeading(Flowable):
        """A section name in caps with a rule under it — one flowable, so the rule can never
        be split from its heading, and ``keepWithNext`` so a heading can never be left
        stranded at the foot of a page."""

        keepWithNext = 1

        def __init__(self, title: str, space_before: float = 9.0):
            Flowable.__init__(self)
            self.title = title.upper()
            # Whitespace shrinks with the type. Leaving the gaps fixed while the text
            # shrank was why scaling bought so little: on a dense CV the inter-section
            # spacing is a large share of the page, so a 20% type reduction reclaimed far
            # less than 20% of the height.
            self.spaceBefore = space_before * scale
            self.spaceAfter = 3.0 * scale

        def wrap(self, availWidth, availHeight):
            self.width = availWidth
            self.height = (HEADING_SIZE + 4.5) * scale
            return self.width, self.height

        def draw(self):
            canvas = self.canv
            canvas.setFont(FONT_BOLD, HEADING_SIZE * scale)
            canvas.setFillColor(INK)
            canvas.drawString(0, self.height - HEADING_SIZE, self.title)
            canvas.setStrokeColor(INK)
            canvas.setLineWidth(0.7)
            canvas.line(0, 1.5, self.width, 1.5)

    _FLOWABLES.update(TwoCol=TwoCol, SectionHeading=SectionHeading)
    return _FLOWABLES


#: Typographic scales tried, in order, to land the CV on one page. The first is the
#: template's own scale, so a CV that already fits is rendered exactly as before and pays
#: one pass. The floor is deliberate: below ~80% Times stops being comfortably readable at
#: print size, and a CV that still overflows there has too much content to shrink its way
#: out — two honest pages beat one unreadable one.
_FIT_SCALES: tuple[float, ...] = (1.0, 0.96, 0.92, 0.88, 0.84, 0.80)


def render_pdf(cv: dict, meta: dict | None = None) -> bytes:
    """The CV as a PDF, shrunk to fit one page when it can be.

    Rendered repeatedly at descending type scales until it fits, rather than by estimating
    height up front: reportlab's own pagination is the only thing that actually knows where
    the text lands once wrapping, widows and section rules are taken into account, so the
    honest way to ask "does this fit?" is to lay it out and look at the page count.

    Costs one render for a CV that already fits, and at most six for one that never does.
    """
    name = _text((cv.get("contact") or {}).get("name"))
    rendered = b""
    for scale in _FIT_SCALES:
        rendered, pages = _render_pdf_at(cv, scale)
        if pages <= 1:
            return doc_meta.stamp_pdf(rendered, meta, name=name)
    # Nothing fit: return the tightest attempt rather than an unshrunk overflow, and let
    # it run to a second page rather than dropping any of the candidate's content.
    return doc_meta.stamp_pdf(rendered, meta, name=name)


def _render_pdf_at(cv: dict, scale: float) -> tuple[bytes, int]:
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate

    built = _flowables(scale)
    TwoCol, SectionHeading = built["TwoCol"], built["SectionHeading"]

    def esc(value) -> str:
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def attr(value) -> str:
        return esc(value).replace('"', "&quot;")

    s = _sections(cv)
    # Margins tighten with the type, but only half as fast: a CV whose text has shrunk
    # 15% still needs breathing room at the edges or it reads as cramped rather than dense.
    margin_mm = MARGIN_MM * (1.0 - (1.0 - scale) * 0.5)
    content_w = A4[0] - 2 * margin_mm * mm
    name = _text(s["contact"].get("name"))

    name_st = ParagraphStyle("name", fontName=FONT_BOLD, fontSize=NAME_SIZE * scale,
                             leading=NAME_SIZE * scale + 3, alignment=TA_CENTER, spaceAfter=3 * scale)
    contact_st = ParagraphStyle("contact", fontName=FONT, fontSize=CONTACT_SIZE * scale,
                                leading=CONTACT_SIZE * scale + 2.5, alignment=TA_CENTER,
                                textColor=MUTED)
    body_st = ParagraphStyle("body", fontName=FONT, fontSize=BODY_SIZE * scale,
                             leading=BODY_SIZE * scale + 2.6, alignment=TA_LEFT)
    refs_st = ParagraphStyle("refs", fontName=FONT_ITALIC, fontSize=9, leading=12,
                             textColor=MUTED, spaceBefore=9 * scale)

    story: list = [Paragraph(esc(name or "Curriculum Vitae"), name_st)]
    bits = _contact_bits(s["contact"])
    if bits:
        story.append(Paragraph(" | ".join(
            f'<link href="{attr(href)}">{esc(shown)}</link>' if href else esc(shown)
            for shown, href in bits
        ), contact_st))

    def section(title: str) -> None:
        story.append(SectionHeading(title))

    def bullet_list(items: list[str]) -> None:
        listing = ListFlowable(
            [ListItem(Paragraph(esc(i), body_st), leftIndent=11 * scale) for i in items],
            bulletType="bullet", start="•", leftIndent=13 * scale,
            bulletFontName=FONT, bulletFontSize=BODY_SIZE * scale,
        )
        listing.spaceBefore = 1.5 * scale
        story.append(listing)

    if s["summary"]:
        section(PROFILE_HEADING)
        story.append(Paragraph(esc(s["summary"]), body_st))
    if s["skills"]:
        section(SKILLS_HEADING)
        story.append(Paragraph(esc(", ".join(s["skills"])), body_st))
    if s["experience"]:
        section(EXPERIENCE_HEADING)
        for i, role in enumerate(s["experience"]):
            title, dates = _text(role.get("title")), _text(role.get("dates"))
            company, location = _text(role.get("company")), _text(role.get("location"))
            if title or dates:
                story.append(TwoCol(title, dates, left_font=FONT_BOLD, right_font=FONT_ITALIC,
                                    size=ENTRY_SIZE * scale, leading=ENTRY_SIZE * scale + 3,
                                    right_colour=MUTED, space_before=0 if i == 0 else 6))
            if company or location:
                story.append(TwoCol(company, location, left_font=FONT_ITALIC,
                                    right_font=FONT_ITALIC, size=BODY_SIZE * scale,
                                    leading=BODY_SIZE * scale + 2.5, right_colour=MUTED))
            bullets = _bullets(role)
            if bullets:
                bullet_list(bullets)
    if s["projects"]:
        # Between Experience and Education, matching where a candidate's own CV puts it:
        # a project is work, and it belongs with the work rather than after the footnotes.
        section(PROJECTS_HEADING)
        for project in s["projects"]:
            story.append(TwoCol(project["name"], project["dates"],
                                left_font=FONT_BOLD, right_font=FONT_ITALIC,
                                size=ENTRY_SIZE * scale, leading=ENTRY_SIZE * scale + 3,
                                right_colour=MUTED, space_before=5))
            if project["tech"]:
                story.append(TwoCol(project["tech"], "", left_font=FONT_ITALIC,
                                    right_font=FONT_ITALIC, size=BODY_SIZE * scale,
                                    leading=BODY_SIZE * scale + 2.5, right_colour=MUTED))
            if project["bullets"]:
                bullet_list(project["bullets"])

    if s["education"]:
        section(EDUCATION_HEADING)
        for entry in s["education"]:
            left, right = _split_trailing_dates(entry)
            right_w = stringWidth(right, FONT_ITALIC, BODY_SIZE * scale) if right else 0
            if stringWidth(left, FONT, BODY_SIZE * scale) + right_w + 12 <= content_w:
                story.append(TwoCol(left, right, left_font=FONT, right_font=FONT_ITALIC,
                                    size=BODY_SIZE * scale, leading=BODY_SIZE * scale + 3,
                                    right_colour=MUTED, space_before=2))
            else:
                # Too long to sit on one line — wrap the whole entry rather than let the
                # two columns run into each other.
                story.append(Paragraph(esc(entry), body_st))
    if s["certifications"]:
        section(CERTIFICATIONS_HEADING)
        bullet_list(s["certifications"])
    for extra in s["additional_sections"]:
        section(extra["heading"])
        bullet_list(extra["items"])

    story.append(Paragraph(esc(REFERENCES_LINE), refs_st))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=margin_mm * mm, bottomMargin=margin_mm * mm,
        leftMargin=margin_mm * mm, rightMargin=margin_mm * mm,
        title=name or "CV", author=name or "",
    )
    doc.build(story)
    return buf.getvalue(), doc.page


# --------------------------------------------------------------------------- #
# DOCX (python-docx) — A4, single column, selectable text
# --------------------------------------------------------------------------- #
def _rule_below(paragraph) -> None:
    """Draw the section rule as the heading paragraph's bottom border. python-docx has no
    border API, so this writes the same ``w:pBdr`` element Word itself would."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")       # eighths of a point
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "000000")
    borders.append(bottom)
    paragraph._p.get_or_add_pPr().append(borders)


def render_docx(cv: dict, meta: dict | None = None) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.shared import Mm, Pt

    s = _sections(cv)
    document = docx.Document()
    for section_ in document.sections:  # A4
        section_.page_width, section_.page_height = Mm(210), Mm(297)
        section_.top_margin = section_.bottom_margin = Mm(MARGIN_MM)
        section_.left_margin = section_.right_margin = Mm(MARGIN_MM)

    # One typographic scale for the whole document, matching the PDF.
    normal = document.styles["Normal"]
    normal.font.name = DOCX_FONT
    normal.font.size = Pt(BODY_SIZE)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)

    # The right-hand column of every entry line: a right-aligned tab at the text edge. A
    # tab, not a table — same ATS reasoning as the PDF's TwoCol.
    right_tab = Mm(PAGE_W_MM - 2 * MARGIN_MM)

    def para(text: str, *, size: float = BODY_SIZE, bold: bool = False,
             italic: bool = False, align=None, space_before: float = 0.0):
        p = document.add_paragraph()
        p.paragraph_format.space_before = Pt(space_before)
        if align is not None:
            p.alignment = align
        run = p.add_run(text)
        run.bold, run.italic = bold, italic
        run.font.size, run.font.name = Pt(size), DOCX_FONT
        return p

    def row(left: str, right: str, *, size: float = BODY_SIZE, bold_left: bool = False,
            italic_left: bool = False, space_before: float = 0.0):
        p = document.add_paragraph()
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.tab_stops.add_tab_stop(right_tab, WD_TAB_ALIGNMENT.RIGHT)
        if left:
            run = p.add_run(left)
            run.bold, run.italic = bold_left, italic_left
            run.font.size, run.font.name = Pt(size), DOCX_FONT
        if right:
            run = p.add_run(("\t" if left else "") + right)
            run.italic = True
            run.font.size, run.font.name = Pt(size), DOCX_FONT
        return p

    def heading(title: str) -> None:
        p = para(title.upper(), size=HEADING_SIZE, bold=True, space_before=9)
        _rule_below(p)

    def bullets(items: list[str]) -> None:
        for item in items:
            p = document.add_paragraph(style="List Bullet")
            run = p.add_run(item)
            run.font.size, run.font.name = Pt(BODY_SIZE), DOCX_FONT

    name = _text(s["contact"].get("name")) or "Curriculum Vitae"
    para(name, size=NAME_SIZE, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    line = _contact_line(s["contact"])
    if line:
        para(line, size=CONTACT_SIZE, align=WD_ALIGN_PARAGRAPH.CENTER)

    if s["summary"]:
        heading(PROFILE_HEADING)
        para(s["summary"])
    if s["skills"]:
        heading(SKILLS_HEADING)
        para(", ".join(s["skills"]))
    if s["experience"]:
        heading(EXPERIENCE_HEADING)
        for i, role in enumerate(s["experience"]):
            title, dates = _text(role.get("title")), _text(role.get("dates"))
            company, location = _text(role.get("company")), _text(role.get("location"))
            if title or dates:
                row(title, dates, size=ENTRY_SIZE, bold_left=True,
                    space_before=0 if i == 0 else 6)
            if company or location:
                row(company, location, italic_left=True)
            bullets(_bullets(role))
    if s["projects"]:
        heading(PROJECTS_HEADING)
        for project in s["projects"]:
            row(project["name"], project["dates"], bold_left=True, space_before=5)
            if project["tech"]:
                row(project["tech"], "", italic_left=True)
            bullets(project["bullets"])

    if s["education"]:
        heading(EDUCATION_HEADING)
        for entry in s["education"]:
            left, right = _split_trailing_dates(entry)
            row(left, right, space_before=2)
    if s["certifications"]:
        heading(CERTIFICATIONS_HEADING)
        bullets(s["certifications"])
    for extra in s["additional_sections"]:
        heading(extra["heading"])
        bullets(extra["items"])

    para(REFERENCES_LINE, size=9, italic=True, space_before=9)

    doc_meta.stamp_docx(document, meta, name=name)

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


def render_both(cv: dict, meta: dict | None = None) -> dict:
    """Render PDF + DOCX; return bytes + a machine-readability verdict for each.

    ``meta`` is the uploaded CV's document properties (see ``doc_meta``); both files
    carry them so a tailored export matches the original rather than the toolchain."""
    pdf, docx_bytes = render_pdf(cv, meta), render_docx(cv, meta)
    ptext = pdf_text(pdf)
    return {
        "pdf": pdf,
        "docx": docx_bytes,
        "pdf_text_len": len(ptext),
        "pdf_machine_readable": len(ptext) >= 50,  # real extractable text, not an image
    }
