"""Jooble connector — RoI coverage via a free-string location (what Adzuna's `ie`
couldn't do). Structured JSON in, deterministic field-mapping out — no LLM."""

from __future__ import annotations

import logging

import httpx
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

logger = logging.getLogger("jobpilot.sources")
JOOBLE_BASE = "https://jooble.org/api"


class JoobleError(RuntimeError):
    """Jooble call failed (missing key or non-200)."""


def _to_raw(j: dict) -> RawJob:
    return RawJob(
        connector="jooble",
        source="jooble",
        source_type="api",
        title=j.get("title"),
        company=j.get("company"),
        location=j.get("location"),
        description=j.get("snippet"),
        url=j.get("link"),
        external_id=str(j["id"]) if j.get("id") is not None else None,
        raw={k: j.get(k) for k in ("salary", "type", "updated", "source") if j.get(k)},
    )


async def search_jooble(
    settings: Settings,
    *,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "Dublin, Ireland",
    page: int = 1,
) -> list[RawJob]:
    """POST to Jooble (key in the path) and return jobs in the common raw shape.
    ``location`` is a free string; we bias to Ireland when only a bare city is given."""
    key = settings.require_jooble_key()
    kw = " ".join([*(roles or []), *(keywords or [])]).strip() or "software"
    loc = location if ("," in location or "ireland" in location.lower()) else f"{location}, Ireland"
    body = {"keywords": kw, "location": loc, "page": str(page)}
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        resp = await client.post(f"{JOOBLE_BASE}/{key}", json=body)
    if resp.status_code != 200:
        raise JoobleError(f"jooble HTTP {resp.status_code}: {resp.text[:160]}")
    jobs = resp.json().get("jobs", [])[: settings.aggregator_results_per_page]
    logger.info("jooble.call location=%r keywords=%r -> %d jobs", loc, kw, len(jobs))
    return [_to_raw(j) for j in jobs]
