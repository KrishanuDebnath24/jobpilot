"""Careerjet connector — RoI coverage via the Ireland site (``locale_code=en_IE``).

Careerjet is an affiliate API: it expects ``affid`` plus ``user_ip``/``user_agent``/
``url`` for attribution. In this backend/batch context we pass the server's
representative IP + a descriptive UA + the app URL, and log them. Structured JSON in,
deterministic field-mapping out — no LLM. Thin httpx client over the public JSON
endpoint (documented at careerjet.com/partners), which keeps output-shape control
versus the third-party ``careerjet-api`` package.
"""

from __future__ import annotations

import logging

import httpx
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

logger = logging.getLogger("jobpilot.sources")
CAREERJET_BASE = "http://public.api.careerjet.net/search"


class CareerjetError(RuntimeError):
    """Careerjet call failed (missing affid or non-200/non-JOBS response)."""


def _to_raw(j: dict) -> RawJob:
    return RawJob(
        connector="careerjet",
        source="careerjet",
        source_type="api",
        title=j.get("title"),
        company=j.get("company"),
        location=j.get("locations"),
        description=j.get("description"),
        url=j.get("url"),
        external_id=None,  # Careerjet has no stable job id; dedupe uses company+title+loc
        raw={k: j.get(k) for k in ("salary", "salary_min", "salary_max", "date", "site") if j.get(k)},
    )


async def search_careerjet(
    settings: Settings,
    *,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "Dublin",
    page: int = 1,
) -> list[RawJob]:
    affid = settings.require_careerjet_affid()
    kw = " ".join([*(roles or []), *(keywords or [])]).strip() or "software"
    params = {
        "affid": affid,
        "keywords": kw,
        "location": location,
        "locale_code": settings.careerjet_locale,
        "sort": "date",
        "pagesize": settings.aggregator_results_per_page,
        "page": page,
        "user_ip": settings.careerjet_user_ip,
        "user_agent": settings.crawler_user_agent,
        "url": settings.frontend_origin,
    }
    logger.info(
        "careerjet.call locale=%s affid=%s… user_ip=%s location=%r",
        settings.careerjet_locale, affid[:4], settings.careerjet_user_ip, location,
    )
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        resp = await client.get(CAREERJET_BASE, params=params)
    if resp.status_code != 200:
        raise CareerjetError(f"careerjet HTTP {resp.status_code}: {resp.text[:160]}")
    data = resp.json()
    if data.get("type") != "JOBS":
        return []  # e.g. a location-disambiguation response -> no jobs this call
    jobs = data.get("jobs", [])[: settings.aggregator_results_per_page]
    return [_to_raw(j) for j in jobs]
