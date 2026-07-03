"""Deterministic resume parsing — NO LLM calls this phase.

Extract text (PDF via pdfplumber, DOCX via python-docx), then a rules-based pass
into :class:`StructuredResume`. Untrusted input: this stays extract-only and never
influences which user_id a query runs under (see mcp_auth). A Phase-2 LLM
structuring pass (Haiku 4.5) can refine the same shape behind this interface.
"""

from __future__ import annotations

import io
import re

from .schemas import Contact, EducationItem, ExperienceItem, StructuredResume

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ALLOWED_MIME = {PDF_MIME, DOCX_MIME}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_LINK_RE = re.compile(r"(?:https?://|www\.)[^\s]+", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*•·▪◦]\s+")

_SECTIONS = {
    "skills": ("skills", "technical skills", "core skills", "technologies"),
    "experience": ("experience", "work experience", "employment", "professional experience"),
    "education": ("education", "academic background", "qualifications"),
}


class UnsupportedResumeType(ValueError):
    pass


def sniff_mime(data: bytes, declared: str | None) -> str:
    if declared in ALLOWED_MIME:
        return declared
    if data[:4] == b"%PDF":
        return PDF_MIME
    if data[:4] == b"PK\x03\x04":  # zip container (docx/xlsx/...) — trust declared docx only
        if declared == DOCX_MIME:
            return DOCX_MIME
    raise UnsupportedResumeType(f"unsupported resume type (declared={declared!r})")


def extract_text(data: bytes, mime: str) -> str:
    if mime == PDF_MIME:
        return _extract_pdf(data)
    if mime == DOCX_MIME:
        return _extract_docx(data)
    raise UnsupportedResumeType(mime)


def _extract_pdf(data: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


def _extract_docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs).strip()


def _classify_header(line: str) -> str | None:
    key = line.strip().lower().rstrip(":")
    if len(key) > 40:
        return None
    for section, names in _SECTIONS.items():
        if key in names:
            return section
    return None


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"_header": []}
    current = "_header"
    for line in lines:
        header = _classify_header(line)
        if header:
            current = header
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def to_structured(raw_text: str) -> StructuredResume:
    lines = [ln.rstrip() for ln in raw_text.splitlines()]
    nonempty = [ln for ln in lines if ln.strip()]
    sections = _split_sections(lines)

    email = next(iter(_EMAIL_RE.findall(raw_text)), None)
    phone = next(iter(_PHONE_RE.findall(raw_text)), None)
    links = list(dict.fromkeys(_LINK_RE.findall(raw_text)))
    name = None
    for ln in nonempty:
        s = ln.strip()
        if "@" in s or _classify_header(s):
            continue
        if 2 <= len(s.split()) <= 6 and not any(ch.isdigit() for ch in s):
            name = s
            break

    contact = Contact(name=name, email=email, phone=phone, links=links)

    skills: list[str] = []
    for ln in sections.get("skills", []):
        for tok in re.split(r"[,/|;•·\t]|\s{2,}", ln):
            tok = tok.strip(" .-")
            if tok:
                skills.append(tok)
    skills = list(dict.fromkeys(skills))

    experience: list[ExperienceItem] = []
    current_item: ExperienceItem | None = None
    for ln in sections.get("experience", []):
        if not ln.strip():
            continue
        if _BULLET_RE.match(ln):
            if current_item is None:
                current_item = ExperienceItem()
                experience.append(current_item)
            current_item.bullets.append(_BULLET_RE.sub("", ln).strip())
        else:
            current_item = ExperienceItem(title=ln.strip())
            experience.append(current_item)

    education = [
        EducationItem(text=ln.strip())
        for ln in sections.get("education", [])
        if ln.strip()
    ]

    return StructuredResume(
        contact=contact,
        skills=skills,
        experience=experience,
        education=education,
    )


def parse_resume_bytes(
    data: bytes, filename: str, declared_mime: str | None
) -> tuple[str, StructuredResume, str]:
    """Return (raw_text, structured_resume, resolved_mime)."""
    mime = sniff_mime(data, declared_mime)
    raw_text = extract_text(data, mime)
    return raw_text, to_structured(raw_text), mime
