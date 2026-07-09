"""ATS board coverage — expand Greenhouse reach beyond a hand-curated list.

Greenhouse's public Job Board API is *per board* (``boards-api.greenhouse.io/v1/
boards/<slug>/jobs``): there is no endpoint to discover who is hiring in a location.
So we widen coverage two ways:

1. :data:`DEFAULT_GREENHOUSE_BOARDS` — a curated set of companies with an Irish
   presence and a public Greenhouse board, merged with the user's configured
   ``target_employers`` so every user gets broad full-text ATS coverage by default.
2. :func:`extract_greenhouse_slugs` — harvest board slugs from the apply links of
   jobs already fetched (e.g. JSearch/aggregator results that link straight to a
   ``greenhouse.io`` posting), so the board list grows from real listings over time.

Slugs are just data mapped into an API path; a bad slug 404s and is recorded as a
per-board error, never fatal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Curated companies with an Irish (mostly Dublin) presence and a public Greenhouse
# board. Verified live against the boards API and pruned to responders. Grows via
# harvesting + the user's own target_employers.
DEFAULT_GREENHOUSE_BOARDS: list[str] = [
    "intercom",
    "stripe",
    "datadog",
    "twilio",
    "toast",
    "letsgetchecked",
    "gitlab",
    "elastic",
    "mongodb",
    "cloudflare",
    "brave",
    "udemy",
    "greenhouse",
    "airbnb",
]

# greenhouse.io apply links come in a few shapes; capture the board slug from each.
_GH_URL_PATTERNS = [
    re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I),
    re.compile(r"job-boards\.greenhouse\.io/([a-z0-9_-]+)", re.I),
    re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I),
    re.compile(r"greenhouse\.io/embed/job_board\?for=([a-z0-9_-]+)", re.I),
]

# Slugs that appear in greenhouse.io paths but are not company boards.
_NOT_A_BOARD = {"embed", "job_board", "v1", "boards", "www", "api"}


def extract_greenhouse_slugs(urls: Iterable[str | None]) -> set[str]:
    """Return the set of Greenhouse board slugs referenced by any of ``urls``."""
    found: set[str] = set()
    for url in urls:
        if not url or "greenhouse.io" not in url.lower():
            continue
        for pat in _GH_URL_PATTERNS:
            m = pat.search(url)
            if m:
                slug = m.group(1).lower().strip()
                if slug and slug not in _NOT_A_BOARD:
                    found.add(slug)
    return found


def merge_boards(
    configured: Iterable[str] | None,
    harvested: Iterable[str] | None,
    *,
    include_defaults: bool = True,
    cap: int = 40,
) -> list[str]:
    """Union of configured employers, the curated defaults, and harvested slugs —
    de-duplicated (case-insensitive), configured-first, capped for politeness/time."""
    seen: set[str] = set()
    out: list[str] = []
    groups = [configured or []]
    if include_defaults:
        groups.append(DEFAULT_GREENHOUSE_BOARDS)
    groups.append(sorted(harvested or []))
    for group in groups:
        for slug in group:
            key = (slug or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
            if len(out) >= cap:
                return out
    return out
