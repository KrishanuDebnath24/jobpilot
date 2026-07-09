"""SmartRecruiters ATS connector — free public Posting API, per company.

Two calls: ``/v1/companies/<id>/postings`` lists a company's live postings (title +
location, no body); ``/v1/companies/<id>/postings/<postingId>`` returns the full
``jobAd`` (description + qualifications). No key required. We filter the list by
role/location first, then fetch the body only for the matches (capped), so a big
board costs a bounded number of detail calls. No LLM.

Speculative-safe: the list endpoint returns 200 with an empty page for an unknown
company id, so an id can come from a curated list OR be harvested from a
``jobs.smartrecruiters.com/<id>`` apply link. Unknown/empty → ``[]``.
"""

from __future__ import annotations

import re

import httpx
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

SR_BASE = "https://api.smartrecruiters.com/v1/companies"


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


def _list_location(posting: dict) -> str:
    loc = posting.get("location") or {}
    return loc.get("fullLocation") or ", ".join(
        x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
    )


def _full_text(detail: dict) -> str:
    """Concatenate the jobAd sections into one description body, capped."""
    sections = ((detail.get("jobAd") or {}).get("sections")) or {}
    order = ("jobDescription", "qualifications", "additionalInformation")
    parts = [(sections.get(k) or {}).get("text") or "" for k in order]
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    return text[:8000]


async def fetch_smartrecruiters(
    settings: Settings,
    *,
    company: str,
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    max_results: int = 50,
) -> list[RawJob]:
    """Fetch a company's SmartRecruiters postings, filter, then pull the full body for
    each match (bounded by ``scout_sr_max_detail_fetches``). ``connector`` =
    ``smartrecruiters:<id>``. Unknown id → []."""
    ident = company.strip()
    terms = _tokens(roles, keywords)
    locs = locations or []
    detail_cap = settings.scout_sr_max_detail_fetches

    out: list[RawJob] = []
    async with httpx.AsyncClient(timeout=settings.crawler_timeout_seconds) as client:
        resp = await client.get(f"{SR_BASE}/{ident}/postings", params={"limit": 100})
        if resp.status_code != 200:
            return []
        postings = resp.json().get("content") or []

        # Filter on the cheap list fields first; only matches cost a detail call.
        matches = [
            p for p in postings
            if _matches(p.get("name") or "", _list_location(p), terms, locs)
        ][:detail_cap]

        for p in matches:
            pid = p.get("id")
            if not pid:
                continue
            det = await client.get(f"{SR_BASE}/{ident}/postings/{pid}")
            description = _full_text(det.json()) if det.status_code == 200 else ""
            company_name = (p.get("company") or {}).get("name") or ident
            out.append(
                RawJob(
                    connector=f"smartrecruiters:{ident}",
                    source=f"smartrecruiters:{ident}",
                    source_type="ats",
                    title=p.get("name"),
                    company=company_name,
                    location=_list_location(p) or None,
                    description=description or None,
                    # Full only when we actually fetched the body; else honest snippet.
                    description_completeness="full" if description else "snippet",
                    url=(det.json().get("postingUrl") if det.status_code == 200 else None)
                    or (det.json().get("applyUrl") if det.status_code == 200 else None),
                    external_id=str(pid),
                    raw={"ref": p.get("refNumber"), "released": p.get("releasedDate")},
                )
            )
            if len(out) >= max_results:
                break
    return out
