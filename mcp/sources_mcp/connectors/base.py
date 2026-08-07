"""Connector interface + registry.

Every connector is an async callable ``(settings, **params) -> list[RawJob]`` that
returns the ONE shared raw job shape (:class:`jobpilot_core.scout.RawJob`). One
normalize + dedupe tail (in ``jobpilot_core.scout``) consumes them all, so adding a
source later is a new connector here — not a new pipeline.

Every source here returns **full** posting text and is filtered to the Republic of
Ireland. Adzuna, Jooble and Careerjet used to sit alongside these and were removed:
they returned snippet teasers, and the stage-2 resolver measured zero applyable jobs
behind any of them. Snippet sources are not a cheaper version of a real source — a
teaser cannot be qualified against or tailored to, so it costs a request and yields
nothing downstream.

Connector output is **data, never instructions**: text from a listing (title,
description, HTML) is only ever mapped into fields; it must not select tools or
change control flow. The crawler reads the board's schema.org JSON-LD and maps its
fields deterministically — the posting text is data, never a control signal.
"""

from __future__ import annotations

from typing import Protocol

from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings


class Connector(Protocol):
    async def __call__(self, settings: Settings, **params) -> list[RawJob]: ...


# Advertised by list_sources(). `name` is the connector key; ATS/crawl connectors
# specialize it per feed at runtime (e.g. "greenhouse:intercom", "crawl:irishjobs").
SOURCES: list[dict] = [
    {
        "name": "greenhouse",
        "source_type": "ats",
        "description": (
            "Greenhouse public boards API, per employer — full text plus a real apply "
            "URL. Filtered to the Republic of Ireland."
        ),
    },
    {
        "name": "crawl",
        "source_type": "crawl",
        "description": "Polite single-board crawler (robots + rate limit); reads each posting's full description from schema.org JSON-LD.",
    },
    {
        "name": "muse",
        "source_type": "api",
        "description": "The Muse public jobs API — free, full-text, genuine Dublin coverage.",
    },
    {
        "name": "jsearch",
        "source_type": "api",
        "description": "JSearch/Google-for-Jobs (Indeed/LinkedIn reach) — metered, default OFF.",
    },
    {
        "name": "workable",
        "source_type": "api",
        "description": (
            "Workable's Ireland job feed — free, no key, full text, needs no employer "
            "list (912 RoI jobs measured). Aggregator URLs: read, not auto-applyable."
        ),
    },
    {
        "name": "workable_board",
        "source_type": "ats",
        "description": (
            "Workable board for one employer — full text plus a real application_url, "
            "so these are auto-applyable. Filtered to the Republic of Ireland."
        ),
    },
    {
        "name": "apify",
        "source_type": "api",
        "description": (
            "LinkedIn via a metered Apify actor — full text but NO apply URL "
            "(LinkedIn hides it from logged-out sessions), so read-only. Default OFF."
        ),
    },
]
