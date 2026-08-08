"""Workable connector — free, no key, full text, and the largest verified RoI pool.

Two modes, because Workable exposes two different public APIs:

``search_workable``   ``jobs.workable.com/api/v1/jobs`` — Workable's own job-seeker feed
                      across every customer, filtered by location. Needs **no employer
                      list at all**, which is what makes it valuable: 912 Irish jobs
                      measured, against 126 across all 18 known Irish Greenhouse boards.
                      New Irish employers appear automatically as they join Workable.

``fetch_workable_board``  ``apply.workable.com/api/v1/widget/accounts/<slug>`` — one
                      employer's board, for slugs discovered by other means. Returns a
                      real ``application_url`` per job, so unlike the feed these are
                      auto-applyable.

Structured JSON in, deterministic field-mapping out — no LLM, same rule as Greenhouse.

Two things learned by probing the live API, both of which would be silent bugs:

- The feed rejects ``limit>20`` with HTTP 400, and its cursor parameter is ``pageToken``.
  Passing ``nextPageToken``/``cursor``/``token`` returns **HTTP 200 with page 0 again**,
  so a wrong name paginates forever over the same 20 jobs instead of erroring.
- A board returns the employer's *entire global* board. Acclaro is 60 jobs with none in
  Ireland; Kepak is 62 with 4. Polling a board without filtering would flood an
  Ireland-only pool, so both modes filter through :func:`jobpilot_core.roi_location.is_roi`.
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup
from jobpilot_core.job_match import match_tokens, title_matches
from jobpilot_core.roi_location import is_roi
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

logger = logging.getLogger("jobpilot.sources")

FEED_BASE = "https://jobs.workable.com/api/v1/jobs"
BOARD_BASE = "https://apply.workable.com/api/v1/widget/accounts"


class WorkableError(RuntimeError):
    """Workable call failed (unknown board or non-200)."""


_tokens = match_tokens
_title_matches = title_matches


def _strip(*html_parts: str | None) -> str:
    """Flatten the posting's HTML sections into one plain-text body.

    Workable splits a posting across ``description``/``requirementsSection``/
    ``benefitsSection``; the requirements are usually the part a tailoring step needs
    most, so joining them matters rather than taking ``description`` alone.
    """
    chunks = []
    for part in html_parts:
        if not part:
            continue
        text = BeautifulSoup(part, "html.parser").get_text(" ", strip=True)
        if text:
            chunks.append(text)
    return re.sub(r"\s+", " ", "\n\n".join(chunks)).strip()


# --------------------------------------------------------------------------- #
# Mode 1: the location feed (no employer list needed)
# --------------------------------------------------------------------------- #
def _feed_location(job: dict) -> tuple[str | None, str | None, str | None]:
    """``(label, country, country_code)`` from a feed record."""
    loc = job.get("location") or {}
    city, country = loc.get("city"), loc.get("countryName")
    # The feed writes "any" for location-agnostic postings; that is not a city.
    parts = [p for p in (None if city == "any" else city, loc.get("subregion"), country) if p]
    code = None
    # `locations` is heterogeneous in live responses — usually objects, sometimes bare
    # strings — so read the country code only from entries that actually carry one.
    for entry in job.get("locations") or []:
        if isinstance(entry, dict):
            code = entry.get("countryCode") or code
    return (", ".join(parts) or None, country, code)


def _feed_to_raw(job: dict, label: str | None) -> RawJob:
    company = job.get("company") or {}
    return RawJob(
        connector="workable",
        source="workable",
        source_type="api",
        title=job.get("title"),
        company=company.get("title"),
        location=label,
        description=_strip(
            job.get("description"), job.get("requirementsSection"), job.get("benefitsSection")
        ) or None,
        description_completeness="full",  # feed carries the whole posting body
        # jobs.workable.com is Workable's aggregator, not an application form. Recorded
        # honestly so ats_detect files it as such rather than implying it is applyable.
        url=job.get("url"),
        external_id=str(job["id"]) if job.get("id") is not None else None,
        raw={
            "workplace": job.get("workplace"),
            "employment_type": job.get("employmentType"),
            "department": job.get("department"),
            "created": job.get("created"),
            "company_website": company.get("website"),
        },
    )


async def search_workable(
    settings: Settings,
    *,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str | None = None,
    max_results: int | None = None,
) -> list[RawJob]:
    """Workable's Ireland feed -> jobs in the common raw shape.

    Paginates by cursor up to ``workable_max_pages``. Every record is checked against
    :func:`is_roi` regardless of the server-side facet, because the facet leaks.
    """
    loc = (location or settings.workable_location).strip()
    terms = _tokens(roles, keywords)
    cap = max_results or (settings.workable_max_pages * settings.workable_page_size)

    out: list[RawJob] = []
    seen: set[str] = set()
    dropped_non_roi = 0
    token: str | None = None

    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        for page in range(settings.workable_max_pages):
            params: dict[str, str] = {"location": loc, "limit": str(settings.workable_page_size)}
            if token:
                params["pageToken"] = token  # NB: any other name silently re-serves page 0
            resp = await client.get(FEED_BASE, params=params)
            if resp.status_code != 200:
                if page == 0:
                    raise WorkableError(f"workable feed HTTP {resp.status_code}: {resp.text[:160]}")
                break
            body = resp.json()
            jobs = body.get("jobs") or []
            if not jobs:
                break
            for job in jobs:
                jid = str(job.get("id") or "")
                if jid and jid in seen:
                    continue
                label, country, code = _feed_location(job)
                if not is_roi(label, country=country, country_code=code):
                    dropped_non_roi += 1
                    continue
                if not _title_matches(job.get("title"), terms):
                    continue
                seen.add(jid)
                out.append(_feed_to_raw(job, label))
                if len(out) >= cap:
                    break
            token = body.get("nextPageToken")
            if len(out) >= cap or not token:
                break

    logger.info(
        "workable.feed location=%r pages<=%d -> %d jobs (%d dropped: not RoI)",
        loc, settings.workable_max_pages, len(out), dropped_non_roi,
    )
    return out


# --------------------------------------------------------------------------- #
# Mode 2: one employer's board
# --------------------------------------------------------------------------- #
def _board_to_raw(job: dict, slug: str, employer: str | None) -> RawJob:
    city = ", ".join(p for p in (job.get("city"), job.get("state"), job.get("country")) if p)
    return RawJob(
        connector=f"workable:{slug}",
        source=f"workable:{slug}",
        source_type="ats",
        title=job.get("title"),
        company=employer,
        location=city or None,
        description=_strip(job.get("description")) or None,
        description_completeness="full",
        # The board hands us a real application form, unlike the aggregator feed.
        url=job.get("application_url") or job.get("url") or job.get("shortlink"),
        external_id=job.get("shortcode") or None,
        raw={
            "shortcode": job.get("shortcode"),
            "department": job.get("department"),
            "employment_type": job.get("employment_type"),
            "telecommuting": job.get("telecommuting"),
            "published_on": job.get("published_on"),
        },
    )


async def fetch_workable_board(
    settings: Settings,
    *,
    company: str,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    max_results: int = 50,
    roi_only: bool = True,
) -> list[RawJob]:
    """One employer's Workable board -> jobs in the common raw shape (ATS).

    ``roi_only`` defaults on: a board is the company's whole global careers page, so
    without it a single US-heavy employer can outnumber every Irish job in the pool.
    """
    slug = company.strip().lower()
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        resp = await client.get(f"{BOARD_BASE}/{slug}", params={"details": "true"})
    if resp.status_code == 404:
        raise WorkableError(f"workable board not found: {slug}")
    if resp.status_code != 200:
        raise WorkableError(f"workable HTTP {resp.status_code}: {resp.text[:160]}")

    body = resp.json()
    employer = body.get("name") or company.strip()
    terms = _tokens(roles, keywords)
    out: list[RawJob] = []
    dropped = 0
    for job in body.get("jobs") or []:
        label = ", ".join(p for p in (job.get("city"), job.get("state"), job.get("country")) if p)
        if roi_only and not is_roi(label, country=job.get("country")):
            dropped += 1
            continue
        if not _title_matches(job.get("title"), terms):
            continue
        out.append(_board_to_raw(job, slug, employer))
        if len(out) >= max_results:
            break

    logger.info("workable.board slug=%s -> %d jobs (%d dropped: not RoI)", slug, len(out), dropped)
    return out
