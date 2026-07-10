"""The Muse connector — free public jobs API, full descriptions.

``https://www.themuse.com/api/public/jobs`` needs no key (an optional ``api_key`` only
raises rate limits). It's a curated board (skews to larger employers) but returns the
**full** posting body and has genuine Dublin coverage. Structured JSON in, deterministic
field-mapping out — no LLM.

Relevance comes from The Muse's ``category`` taxonomy (a free-text title match is too
strict — most Dublin postings don't repeat the role word in the title). We derive
categories from the user's roles/skills and pass them; the location filter is loose
(it leaks Flexible/Remote into a city query) so we still filter to the requested city
client-side. No categories derived → a broad city query (relevance left to the Qualifier).
"""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

logger = logging.getLogger("jobpilot.sources")
MUSE_BASE = "https://www.themuse.com/api/public/jobs"

# Map role/skill tokens -> The Muse category names (verified live against the API).
# Only categories that actually return results are used; unmapped roles fall back to
# a broad city query.
_CATEGORY_MAP: dict[str, str] = {
    "software": "Software Engineering", "engineer": "Software Engineering",
    "developer": "Software Engineering", "backend": "Software Engineering",
    "frontend": "Software Engineering", "fullstack": "Software Engineering",
    "full-stack": "Software Engineering", "platform": "Software Engineering",
    "devops": "Software Engineering", "sre": "Software Engineering",
    "data": "Data and Analytics", "analytics": "Data and Analytics",
    "analyst": "Data and Analytics", "ml": "Data and Analytics",
    "product": "Product Management",
    "design": "Design and UX", "ux": "Design and UX", "ui": "Design and UX",
}


def _tokens(*groups: list[str] | None) -> set[str]:
    out: set[str] = set()
    for group in groups:
        for phrase in group or []:
            out.update(w for w in re.split(r"\W+", phrase.lower()) if len(w) > 2)
    return out


def _derive_categories(roles: list[str] | None, keywords: list[str] | None) -> list[str]:
    """Distinct Muse categories implied by the role/skill tokens, order-preserved."""
    cats: list[str] = []
    for tok in _tokens(roles, keywords):
        cat = _CATEGORY_MAP.get(tok)
        if cat and cat not in cats:
            cats.append(cat)
    return cats


def _normalize_location(location: str | None) -> str:
    """The Muse's location facet is "City, Country"; a bare city ("Dublin") doesn't
    match and returns global results. The app targets the Republic of Ireland, so
    complete a country-less location to Ireland before querying."""
    loc = (location or "").strip()
    return f"{loc}, Ireland" if loc and "," not in loc else loc


def _locations(job: dict) -> str:
    return ", ".join(loc.get("name", "") for loc in job.get("locations") or [] if loc.get("name"))


def _city_match(job_locations: str, city: str) -> bool:
    return not city or city in job_locations.lower()


def _strip(html: str | None) -> str:
    if not html:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True)).strip()


def _to_raw(job: dict) -> RawJob:
    return RawJob(
        connector="muse",
        source="muse",
        source_type="api",
        title=job.get("name"),
        company=(job.get("company") or {}).get("name"),
        location=_locations(job) or None,
        description=_strip(job.get("contents"))[:8000] or None,
        description_completeness="full",  # The Muse returns the full posting body
        url=(job.get("refs") or {}).get("landing_page"),
        external_id=str(job["id"]) if job.get("id") is not None else None,
        raw={
            "publication_date": job.get("publication_date"),
            "categories": [c.get("name") for c in job.get("categories") or []],
            "levels": [lv.get("name") for lv in job.get("levels") or []],
        },
    )


async def search_muse(
    settings: Settings,
    *,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "Dublin, Ireland",
    max_pages: int | None = None,
) -> list[RawJob]:
    """Fetch The Muse jobs for a location, filtered to the requested city and (when the
    profile implies them) relevant categories. Returns the common raw shape, paginated
    up to ``muse_max_pages``."""
    location = _normalize_location(location)
    city = location.split(",")[0].strip().lower()
    categories = _derive_categories(roles, keywords)
    pages = min(max_pages or settings.muse_max_pages, settings.muse_max_pages)
    base_params: dict = {"location": location}
    if categories:
        base_params["category"] = categories  # repeated param -> Muse OR-matches them
    if settings.muse_api_key:
        base_params["api_key"] = settings.muse_api_key

    out: list[RawJob] = []
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        for page in range(1, pages + 1):
            resp = await client.get(MUSE_BASE, params={**base_params, "page": str(page)})
            if resp.status_code != 200:
                break
            body = resp.json()
            results = body.get("results") or []
            if not results:
                break
            out.extend(_to_raw(job) for job in results if _city_match(_locations(job), city))
            if page >= (body.get("page_count") or page):
                break
    logger.info(
        "muse.call location=%r categories=%s pages=%d -> %d jobs (city-filtered)",
        location, categories, pages, len(out),
    )
    return out
