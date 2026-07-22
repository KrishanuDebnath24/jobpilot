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
    dates: str | None = None
    bullets: list[str] = Field(default_factory=list)


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
