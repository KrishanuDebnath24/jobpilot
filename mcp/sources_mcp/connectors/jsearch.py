"""JSearch connector — Google-for-Jobs reach (aggregates Indeed, LinkedIn,
Glassdoor, …), the compliant path to sources we can't hit directly.

Provider-agnostic: prefers OpenWeb Ninja (the canonical JSearch source, X-API-Key)
and falls back to RapidAPI — both return the same job field shape, and the wrapper
here handles both response envelopes (``data`` as a list vs ``{jobs, cursor}``).

This is the one metered source: default-OFF in the search config, a hard
``jsearch_max_pages`` cap, and every call logged against quota. No LLM.

NOTE: Google-for-Jobs currently returns **no Republic-of-Ireland results** via this
API (verified: gb/de/us return jobs, ie returns 0). RoI coverage comes from Jooble/
Careerjet/crawler; JSearch is kept for other markets + future coverage.
"""

from __future__ import annotations

import logging

import httpx
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

logger = logging.getLogger("jobpilot.sources")


class JSearchError(RuntimeError):
    """JSearch call failed (missing key or non-200)."""


def _to_raw(j: dict) -> RawJob:
    city, country = j.get("job_city"), j.get("job_country")
    location = ", ".join([x for x in (city, country) if x]) or j.get("job_location")
    return RawJob(
        connector="jsearch",
        source="jsearch",
        source_type="api",
        title=j.get("job_title"),
        company=j.get("employer_name"),
        location=location,
        description=j.get("job_description"),
        url=j.get("job_apply_link"),
        external_id=j.get("job_id"),
        raw={
            k: j.get(k)
            for k in ("job_posted_at_datetime_utc", "job_publisher", "job_employment_type")
            if j.get(k)
        },
    )


async def search_jsearch(
    settings: Settings,
    *,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "Dublin, Ireland",
    num_pages: int | None = None,
) -> list[RawJob]:
    """Query JSearch (``country=ie``). Bounded by ``jsearch_max_pages``; each call is
    logged (it consumes provider quota)."""
    provider, url, headers = settings.jsearch_provider()
    kw = " ".join([*(roles or []), *(keywords or [])]).strip() or "software engineer"
    query = f"{kw} in {location}"
    pages = min(num_pages or settings.jsearch_max_pages, settings.jsearch_max_pages)
    params = {
        "query": query,
        "country": settings.jsearch_country,
        "page": "1",
        "num_pages": str(pages),
        "date_posted": settings.jsearch_date_posted,
    }
    logger.info(
        "jsearch.call provider=%s query=%r country=%s num_pages=%d (quota)",
        provider, query, settings.jsearch_country, pages,
    )
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        resp = await client.get(url, params=params, headers=headers)
    if resp.status_code != 200:
        raise JSearchError(f"jsearch({provider}) HTTP {resp.status_code}: {resp.text[:160]}")
    data = resp.json().get("data")
    # RapidAPI returns data as a list; OpenWeb Ninja returns {"jobs": [...], "cursor": ...}
    jobs = data.get("jobs", []) if isinstance(data, dict) else (data or [])
    return [_to_raw(j) for j in jobs]
