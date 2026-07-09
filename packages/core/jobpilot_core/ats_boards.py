"""ATS board coverage — expand reach across the free per-company ATS APIs.

Greenhouse, Lever and SmartRecruiters all expose a **free, per-company** postings API
with full descriptions, but none offers a "who is hiring in a location" discovery
endpoint. So for each we widen coverage two ways:

1. A curated default board list (verified live, pruned to responders) merged with the
   user's configured employers, so every user gets full-text ATS coverage by default.
2. :func:`extract_greenhouse_slugs` / :func:`extract_lever_slugs` /
   :func:`extract_smartrecruiters_slugs` — harvest board slugs from the apply links of
   jobs already fetched, so the board list grows from real listings over time.

Slugs are just data mapped into an API path; an unknown one yields no jobs (the Lever/
SmartRecruiters connectors are speculative-safe and return [] rather than raising).

Note: Greenhouse/Lever slugs are lowercase; **SmartRecruiters company identifiers are
case-sensitive** (e.g. ``DeliveryHero``), so their merge/harvest preserve case.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# --------------------------------------------------------------------------- #
# Curated defaults (verified live; pruned to boards that responded with jobs).
# Grow via harvesting + the user's own employer list. Irish coverage on Lever /
# SmartRecruiters is thin (most Dublin firms use Greenhouse), so those seeds are
# small examples — real coverage comes from harvest + configured companies.
# --------------------------------------------------------------------------- #
DEFAULT_GREENHOUSE_BOARDS: list[str] = [
    "intercom", "stripe", "datadog", "twilio", "toast", "letsgetchecked", "gitlab",
    "elastic", "mongodb", "cloudflare", "brave", "udemy", "greenhouse", "airbnb",
]
DEFAULT_LEVER_BOARDS: list[str] = ["whoop", "gopuff"]
DEFAULT_SMARTRECRUITERS_BOARDS: list[str] = ["DeliveryHero"]

# Apply-link shapes → board slug / company id.
_GH_URL_PATTERNS = [
    re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I),
    re.compile(r"job-boards\.greenhouse\.io/([a-z0-9_-]+)", re.I),
    re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I),
    re.compile(r"greenhouse\.io/embed/job_board\?for=([a-z0-9_-]+)", re.I),
]
_LEVER_URL_PATTERNS = [
    re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I),
    re.compile(r"api\.lever\.co/v0/postings/([a-z0-9_-]+)", re.I),
]
# SmartRecruiters id is case-sensitive → no re.I on the capture group.
_SR_URL_PATTERNS = [
    re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)"),
]

_NOT_A_BOARD = {"embed", "job_board", "v1", "boards", "www", "api", "postings", "companies"}


def _harvest(urls: Iterable[str | None], host: str, patterns, *, casefold: bool) -> set[str]:
    found: set[str] = set()
    for url in urls:
        if not url or host not in url.lower():
            continue
        for pat in patterns:
            m = pat.search(url)
            if m:
                slug = m.group(1).strip()
                key = slug.lower() if casefold else slug
                if slug and slug.lower() not in _NOT_A_BOARD:
                    found.add(key)
    return found


def extract_greenhouse_slugs(urls: Iterable[str | None]) -> set[str]:
    """Greenhouse board slugs referenced by any of ``urls`` (lowercased)."""
    return _harvest(urls, "greenhouse.io", _GH_URL_PATTERNS, casefold=True)


def extract_lever_slugs(urls: Iterable[str | None]) -> set[str]:
    """Lever board slugs referenced by any of ``urls`` (lowercased)."""
    return _harvest(urls, "lever.co", _LEVER_URL_PATTERNS, casefold=True)


def extract_smartrecruiters_slugs(urls: Iterable[str | None]) -> set[str]:
    """SmartRecruiters company ids referenced by any of ``urls`` (case preserved)."""
    return _harvest(urls, "smartrecruiters.com", _SR_URL_PATTERNS, casefold=False)


def merge_boards(
    configured: Iterable[str] | None,
    harvested: Iterable[str] | None,
    defaults: Iterable[str],
    *,
    cap: int = 40,
    casefold: bool = True,
) -> list[str]:
    """Union of configured employers, the curated ``defaults``, and harvested slugs —
    de-duplicated, configured-first, capped for politeness/time. ``casefold`` lowercases
    slugs (Greenhouse/Lever); pass ``False`` for SmartRecruiters' case-sensitive ids."""
    seen: set[str] = set()
    out: list[str] = []
    for group in (configured or [], defaults, sorted(harvested or [])):
        for slug in group:
            slug = (slug or "").strip()
            value = slug.lower() if casefold else slug
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                out.append(value)
            if len(out) >= cap:
                return out
    return out
