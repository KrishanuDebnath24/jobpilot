"""Adzuna connector — a thin async client over the free developer API.

Adzuna returns already-structured JSON, so this is deterministic field-mapping —
**no LLM** (spending tokens to reshape clean JSON is the phase's cost trap). Calls
are bounded (``adzuna_max_pages``) to respect the free-tier quota.
"""

from __future__ import annotations

import httpx
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"

# Fields worth keeping from the payload (kept small; personal data is not harvested).
_RAW_KEYS = (
    "id", "salary_min", "salary_max", "category", "created",
    "contract_time", "contract_type", "adref",
)


class AdzunaError(RuntimeError):
    """Adzuna call failed (missing creds or non-200)."""


def _to_raw(r: dict) -> RawJob:
    return RawJob(
        connector="adzuna",
        source="adzuna",
        source_type="api",
        title=r.get("title"),
        company=(r.get("company") or {}).get("display_name"),
        location=(r.get("location") or {}).get("display_name"),
        description=r.get("description"),
        url=r.get("redirect_url"),
        external_id=str(r["id"]) if r.get("id") is not None else None,
        raw={k: r.get(k) for k in _RAW_KEYS if r.get(k) is not None},
    )


async def search_adzuna(
    settings: Settings,
    *,
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    max_results: int | None = None,
) -> list[RawJob]:
    """Query Adzuna (country = ``adzuna_country``) once per location (bounded by
    ``adzuna_max_locations`` to respect the free-tier quota) and return jobs in the
    common raw shape. Locations Adzuna doesn't cover simply yield nothing."""
    if not settings.adzuna_app_id or not settings.adzuna_app_key:
        raise AdzunaError("ADZUNA_APP_ID / ADZUNA_APP_KEY are not configured")

    what_or = " ".join([*(roles or []), *(keywords or [])]).strip()
    locs = (locations or ["Ireland"])[: settings.adzuna_max_locations]
    per_page = min(max_results or settings.adzuna_results_per_page, settings.adzuna_results_per_page)
    country = settings.adzuna_country

    out: list[RawJob] = []
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        for where in locs:
            params = {
                "app_id": settings.adzuna_app_id,
                "app_key": settings.adzuna_app_key,
                "results_per_page": per_page,
                "where": where or "Ireland",
                "content-type": "application/json",
            }
            if what_or:
                params["what_or"] = what_or
            resp = await client.get(f"{ADZUNA_BASE}/{country}/search/1", params=params)
            if resp.status_code != 200:
                raise AdzunaError(f"adzuna HTTP {resp.status_code}: {resp.text[:200]}")
            out.extend(_to_raw(r) for r in resp.json().get("results", []))
    return out
