"""Greenhouse ATS connector — public boards API, per target employer.

Structured JSON in, deterministic field-mapping out — **no LLM** (a board is already
structured; paying a model to re-read it buys nothing).

A Greenhouse board is the employer's *entire global* careers page, so filtering is the
whole job here. Two filters apply, and they answer different questions:

- ``is_roi`` — is this job in the Republic of Ireland? Enforced by default, because this
  app targets the RoI and several curated boards (Intercom, Stripe, Datadog, Cloudflare)
  are overwhelmingly non-Irish. Note the configured-location filter below cannot do this
  job: it passes *everything* when no locations are configured.
- the configured locations/role tokens — is this the kind of job the user asked for?
"""

from __future__ import annotations

import html
import logging
import re

import httpx
from bs4 import BeautifulSoup
from jobpilot_core.job_match import location_matches, match_tokens, title_matches
from jobpilot_core.roi_location import is_roi
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

logger = logging.getLogger("jobpilot.sources")
GH_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseError(RuntimeError):
    """Greenhouse call failed (unknown board or non-200)."""


_tokens = match_tokens


def _matches(title: str, location: str, terms: set[str], locations: list[str]) -> bool:
    return location_matches(location, locations) and title_matches(title, terms)


def _text(content_html: str | None, limit: int) -> str:
    """Plain text of the posting body, capped at ``limit`` characters.

    The cap used to be 6,000, which silently cut real postings: of the verified Irish
    boards, Tines reaches 9,872 characters, Amach 8,387 and Nearform 7,686. Requirements
    usually sit late in a posting, so a low cap removes exactly the part that qualifying
    and CV tailoring depend on.
    """
    if not content_html:
        return ""
    text = BeautifulSoup(html.unescape(content_html), "html.parser").get_text(" ", strip=True)
    return text[:limit]


async def fetch_greenhouse(
    settings: Settings,
    *,
    company: str,
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    max_results: int = 50,
    roi_only: bool = True,
) -> list[RawJob]:
    """Fetch a company's Greenhouse board, filter to relevant roles/locations, and
    return jobs in the common raw shape. ``connector`` = ``greenhouse:<company>``.

    ``roi_only`` defaults on: a board is the whole global careers page, so without it a
    single US-heavy employer outnumbers every Irish job in the pool.
    """
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
    dropped_non_roi = 0
    for job in resp.json().get("jobs", []):
        location = (job.get("location") or {}).get("name")
        if roi_only and not is_roi(location):
            dropped_non_roi += 1
            continue
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
                description=_text(job.get("content"), settings.ats_max_text_chars),
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
    logger.info(
        "greenhouse.board slug=%s -> %d jobs (%d dropped: not RoI)", slug, len(out), dropped_non_roi
    )
    return out
