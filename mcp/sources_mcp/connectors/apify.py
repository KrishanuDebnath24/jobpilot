"""Apify connector — LinkedIn listings via a rented Apify actor. Metered, default OFF.

Be clear about what this source is and isn't, because it looks more useful than it is:

- It **does** return the full posting body (median 2,145 plain chars over a live
  202-job Irish sample), so it clears the "no snippets in the dashboard" bar and is
  usable for qualifying and CV tailoring.
- It does **not** return an apply URL. Verified twice: ``applyUrl`` was empty on 202 of
  202 records, and LinkedIn's own logged-out guest endpoint carries no apply link at
  all — the destination is shown only to authenticated sessions. So the ``url`` here is
  a ``linkedin.com`` address, which :mod:`jobpilot_core.ats_detect` correctly files as
  ``aggregator``: these jobs are applied to by hand, never by the apply agent.

Cost and consent, both deliberate:

- Metered at roughly $1 per 1,000 results, so this gets the same treatment as JSearch —
  no token means disabled, and ``apify_max_results`` is a hard per-run ceiling rather
  than a default that a caller can talk upward.
- Scraping LinkedIn breaches their User Agreement. That is the operator's call to make,
  not a default this app ships with, which is why it stays off until a token is set.
  Note it sits alongside a deliberate decision elsewhere (see the stage-2 resolver) not
  to work around bot protection on sites that refuse us directly.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

import httpx
from jobpilot_core.roi_location import is_roi
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

logger = logging.getLogger("jobpilot.sources")

APIFY_BASE = "https://api.apify.com/v2/acts"
#: LinkedIn's "posted in the last week" facet — keeps a metered run on fresh listings.
_LAST_WEEK = "r604800"


class ApifyError(RuntimeError):
    """Apify call failed (no token, actor error, or non-200)."""


def _search_url(roles: list[str] | None, keywords: list[str] | None, location: str) -> str:
    """A LinkedIn jobs search URL for the actor to walk."""
    terms = [t.strip() for t in (roles or []) + (keywords or []) if t and t.strip()]
    params = {"location": location, "f_TPR": _LAST_WEEK}
    if terms:
        params["keywords"] = " OR ".join(dict.fromkeys(terms))
    return f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"


def _to_raw(rec: dict) -> RawJob:
    text = (rec.get("descriptionText") or "").strip()
    return RawJob(
        connector="apify:linkedin",
        source="apify:linkedin",
        source_type="api",
        title=rec.get("title"),
        company=rec.get("companyName"),
        location=rec.get("location"),
        description=re.sub(r"\s+", " ", text) or None,
        # Full posting body — but see the module docstring: full text, no apply target.
        description_completeness="full" if text else "snippet",
        url=rec.get("link"),
        external_id=str(rec["id"]) if rec.get("id") is not None else None,
        raw={
            "posted_at": rec.get("postedAt"),
            "employment_type": rec.get("employmentType"),
            "seniority": rec.get("seniorityLevel"),
            "job_function": rec.get("jobFunction"),
            "industries": rec.get("industries"),
            "applicants": rec.get("applicantsCount"),
            "company_linkedin": rec.get("companyLinkedinUrl"),
        },
    )


async def search_apify_linkedin(
    settings: Settings,
    *,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str | None = None,
    max_results: int | None = None,
) -> list[RawJob]:
    """Run the LinkedIn actor once and map its dataset into the common raw shape.

    ``max_results`` is clamped to ``apify_max_results``; a caller cannot raise the ceiling,
    only lower it. Every record is re-checked with :func:`is_roi` because LinkedIn's
    location facet leaks across the border — a live ``location=Ireland`` run returned
    "NHS Ayrshire & Arran" and other UK employers.
    """
    token = (settings.apify_api_token or "").strip()
    if not token:
        raise ApifyError("APIFY_API_TOKEN is not configured (apify connector is off by default)")

    loc = (location or settings.apify_country).strip()
    count = min(max_results or settings.apify_max_results, settings.apify_max_results)
    payload = {
        "urls": [_search_url(roles, keywords, loc)],
        "count": count,
        "scrapeCompany": False,       # we map none of it; not worth the compute
        "autoConvertToAiSearch": False,  # keep the query literally as constructed
        "splitByLocation": False,
    }

    url = f"{APIFY_BASE}/{settings.apify_linkedin_actor}/run-sync-get-dataset-items"
    async with httpx.AsyncClient(timeout=settings.apify_timeout_seconds) as client:
        resp = await client.post(
            url,
            params={"token": token, "timeout": int(settings.apify_timeout_seconds)},
            json=payload,
        )
    if resp.status_code not in (200, 201):
        raise ApifyError(f"apify HTTP {resp.status_code}: {resp.text[:200]}")

    records = resp.json()
    if not isinstance(records, list):
        raise ApifyError(f"apify returned {type(records).__name__}, expected a dataset list")

    out: list[RawJob] = []
    dropped_non_roi = 0
    for rec in records:
        if not is_roi(rec.get("location")):
            dropped_non_roi += 1
            continue
        out.append(_to_raw(rec))

    logger.info(
        "apify.linkedin location=%r requested=%d -> %d records, %d jobs (%d dropped: not RoI)",
        loc, count, len(records), len(out), dropped_non_roi,
    )
    return out
