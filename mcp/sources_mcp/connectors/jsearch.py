"""JSearch connector (RapidAPI) — Google-for-Jobs reach (aggregates Indeed,
LinkedIn, Glassdoor, …), the compliant path to sources we can't hit directly.

This is the one metered source: default-OFF in the search config, a hard
``jsearch_max_pages`` cap, and every call logged against the RapidAPI quota.
Structured JSON in, deterministic field-mapping out — no LLM.
"""

from __future__ import annotations

import logging

import httpx
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

logger = logging.getLogger("jobpilot.sources")
JSEARCH_BASE = "https://jsearch.p.rapidapi.com/search"


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
    """Query JSearch for RoI jobs (``country=ie``). Bounded by ``jsearch_max_pages``;
    each call is logged (it consumes RapidAPI quota)."""
    key = settings.require_rapidapi_key()
    kw = " ".join([*(roles or []), *(keywords or [])]).strip() or "software engineer"
    query = f"{kw} in {location}"
    pages = min(num_pages or settings.jsearch_max_pages, settings.jsearch_max_pages)
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": settings.jsearch_host}
    params = {
        "query": query,
        "country": settings.jsearch_country,
        "page": "1",
        "num_pages": str(pages),
        "date_posted": settings.jsearch_date_posted,
    }
    logger.info(
        "jsearch.call query=%r country=%s num_pages=%d (RapidAPI quota)",
        query, settings.jsearch_country, pages,
    )
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        resp = await client.get(JSEARCH_BASE, params=params, headers=headers)
    if resp.status_code != 200:
        raise JSearchError(f"jsearch HTTP {resp.status_code}: {resp.text[:160]}")
    return [_to_raw(j) for j in resp.json().get("data", [])]
