"""Greenhouse ATS connector — public boards API, per target employer.

Structured JSON in, deterministic field-mapping out — **no LLM** (same cost-trap
rule as Adzuna). Filters the board to the config's locations + role/keyword tokens
so we land relevant roles (e.g. Dublin engineering) rather than the whole board.
"""

from __future__ import annotations

import html
import re

import httpx
from bs4 import BeautifulSoup
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

GH_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseError(RuntimeError):
    """Greenhouse call failed (unknown board or non-200)."""


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


def _text(content_html: str | None) -> str:
    if not content_html:
        return ""
    return BeautifulSoup(html.unescape(content_html), "html.parser").get_text(" ", strip=True)[:6000]


async def fetch_greenhouse(
    settings: Settings,
    *,
    company: str,
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    max_results: int = 50,
) -> list[RawJob]:
    """Fetch a company's Greenhouse board, filter to relevant roles/locations, and
    return jobs in the common raw shape. ``connector`` = ``greenhouse:<company>``."""
    slug = company.strip().lower()
    display = company.strip().capitalize()
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        resp = await client.get(f"{GH_BASE}/{slug}/jobs", params={"content": "true"})
    if resp.status_code == 404:
        raise GreenhouseError(f"greenhouse board not found: {slug}")
    if resp.status_code != 200:
        raise GreenhouseError(f"greenhouse HTTP {resp.status_code}: {resp.text[:160]}")

    terms = _tokens(roles, keywords)
    locs = locations or []
    out: list[RawJob] = []
    for job in resp.json().get("jobs", []):
        location = (job.get("location") or {}).get("name")
        if not _matches(job.get("title") or "", location or "", terms, locs):
            continue
        out.append(
            RawJob(
                connector=f"greenhouse:{slug}",
                source=f"greenhouse:{slug}",
                source_type="ats",
                title=job.get("title"),
                company=display,
                location=location,
                description=_text(job.get("content")),
                description_completeness="full",  # Greenhouse ATS serves the full posting body
                url=job.get("absolute_url"),
                external_id=str(job["id"]) if job.get("id") is not None else None,
                raw={
                    "id": job.get("id"),
                    "updated_at": job.get("updated_at"),
                    "departments": [d.get("name") for d in job.get("departments", [])],
                    "offices": [o.get("name") for o in job.get("offices", [])],
                },
            )
        )
        if len(out) >= max_results:
            break
    return out
