"""Lever ATS connector — free public postings API, per company.

``https://api.lever.co/v0/postings/<slug>?mode=json`` returns a company's published
jobs with full descriptions, no key required (same model as Greenhouse). Structured
JSON in, deterministic field-mapping out — no LLM. Filtered to the config's
locations + role/keyword tokens so we keep relevant roles rather than the whole board.

Speculative-safe: an unknown slug 404s and yields ``[]`` (never raises), so the slug
can come from a curated list OR be harvested from a ``jobs.lever.co/<slug>`` apply link.
"""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

LEVER_BASE = "https://api.lever.co/v0/postings"


def _tokens(*groups: list[str] | None) -> set[str]:
    out: set[str] = set()
    for group in groups:
        for phrase in group or []:
            out.update(w for w in re.split(r"\W+", phrase.lower()) if len(w) > 2)
    return out


def _matches(title: str, location: str, terms: set[str], locations: list[str]) -> bool:
    loc_ok = not locations or any(loc.lower() in (location or "").lower() for loc in locations)
    term_ok = not terms or any(t in (title or "").lower() for t in terms)
    return loc_ok and term_ok


def _strip(html_or_text: str | None) -> str:
    if not html_or_text:
        return ""
    return BeautifulSoup(html_or_text, "html.parser").get_text(" ", strip=True)


def _full_text(job: dict) -> str:
    """Assemble the full posting body: opening + each list section + closing. Lever
    exposes these as HTML (``description``/``lists``/``additional``); the ``*Plain``
    variants are sometimes empty, so we strip the HTML ourselves and cap."""
    parts: list[str] = [_strip(job.get("description"))]
    for lst in job.get("lists") or []:
        head = _strip(lst.get("text"))
        body = _strip(lst.get("content"))
        if head or body:
            parts.append(f"{head}: {body}" if head else body)
    parts.append(_strip(job.get("additional")))
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    return text[:8000]


def _location(job: dict) -> str | None:
    cats = job.get("categories") or {}
    return cats.get("location") or next(iter(cats.get("allLocations") or []), None)


async def fetch_lever(
    settings: Settings,
    *,
    company: str,
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    max_results: int = 50,
) -> list[RawJob]:
    """Fetch a company's Lever board, filter to relevant roles/locations, and return
    jobs in the common raw shape. ``connector`` = ``lever:<slug>``. Unknown slug → []."""
    slug = company.strip().lower()
    display = company.strip().capitalize()
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        resp = await client.get(f"{LEVER_BASE}/{slug}", params={"mode": "json"})
    if resp.status_code in (404, 400):
        return []  # company not on Lever — speculative-safe
    if resp.status_code != 200:
        return []

    terms = _tokens(roles, keywords)
    locs = locations or []
    out: list[RawJob] = []
    for job in resp.json():
        location = _location(job)
        if not _matches(job.get("text") or "", location or "", terms, locs):
            continue
        out.append(
            RawJob(
                connector=f"lever:{slug}",
                source=f"lever:{slug}",
                source_type="ats",
                title=job.get("text"),
                company=display,
                location=location,
                description=_full_text(job),
                description_completeness="full",  # Lever serves the full posting body
                url=job.get("hostedUrl") or job.get("applyUrl"),
                external_id=str(job["id"]) if job.get("id") is not None else None,
                raw={
                    "id": job.get("id"),
                    "team": (job.get("categories") or {}).get("team"),
                    "commitment": (job.get("categories") or {}).get("commitment"),
                    "workplaceType": job.get("workplaceType"),
                },
            )
        )
        if len(out) >= max_results:
            break
    return out
