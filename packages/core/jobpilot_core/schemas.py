"""Typed shapes shared across the backend and MCP services."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Contact(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)


class ExperienceItem(BaseModel):
    title: str | None = None
    company: str | None = None
    location: str | None = None  # where the role was based; printed opposite the employer
    dates: str | None = None
    bullets: list[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    """One project, with the same shape as a role because it is the same kind of thing.

    A project entry on a real CV is a name, the stack it was built with, a date range and
    a set of bullets — structurally identical to :class:`ExperienceItem`. Flattening that
    into a list of strings (which is what ``AdditionalSection`` does) throws the structure
    away at parse time and makes it unrecoverable at render time: the title line becomes
    just another bullet, and the dates end up inline instead of right-aligned.

    ``tech`` is kept separate from ``name`` rather than left in one "Name | Python, Docker"
    string, so the renderer can set it on its own line in italics without having to guess
    where the separator was.
    """

    name: str
    tech: str | None = None
    dates: str | None = None
    bullets: list[str] = Field(default_factory=list)


class AdditionalSection(BaseModel):
    """A headed section the CV carries beyond the typed ones — Publications, Languages,
    Volunteering. Captured so tailoring and the render can never silently drop it.

    Projects deliberately do NOT come through here any more: they have their own typed
    field. This remains the catch-all for sections whose content genuinely is a flat list.
    """

    heading: str
    items: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    text: str


class StructuredResume(BaseModel):
    """The typed target shape produced by deterministic parsing (Phase 1).

    A Phase-2 LLM structuring pass (Haiku 4.5) can refine this behind the same
    interface without changing the shape.
    """

    contact: Contact = Field(default_factory=Contact)
    summary: str | None = None
    total_years_experience: float | None = None  # LLM-computed from work roles (excl. education)
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    #: Typed projects, rendered between Experience and Education. Empty for a CV that has
    #: no Projects section, which is why it needs no separate "has projects" flag.
    projects: list[ProjectItem] = Field(default_factory=list)
    additional_sections: list[AdditionalSection] = Field(default_factory=list)


class JobIn(BaseModel):
    """Input shape for upsert_jobs (Scout will emit this shape in Phase 2)."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    description: str | None = None
    url: str | None = None
    source: str | None = None
    source_type: str | None = None  # api|ats|crawl|seed
    connector: str | None = None
    description_completeness: str = "snippet"  # full|snippet|hydrated (set per source)
    # Deterministically classified by Scout (no LLM); None when unknown. See scout.classify_job.
    employment_type: str | None = None  # full_time|part_time|contract|temporary|internship
    seniority: str | None = None  # intern|junior|mid|senior|staff|principal|lead|manager|director
    dedupe_hash: str | None = None
    raw_json: dict | None = None
    normalized_json: dict | None = None
    status: str = "scouted"
