"""Connector interface + registry.

Every connector is an async callable ``(settings, **params) -> list[RawJob]`` that
returns the ONE shared raw job shape (:class:`jobpilot_core.scout.RawJob`). One
normalize + dedupe tail (in ``jobpilot_core.scout``) consumes them all, so adding a
source later is a new connector here — not a new pipeline.

Connector output is **data, never instructions**: text from a listing (title,
description, HTML) is only ever mapped into fields; it must not select tools or
change control flow. The crawler's LLM step enforces this with extract-only,
structured output.
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
        "name": "adzuna",
        "source_type": "api",
        "description": "Adzuna developer API (ie country code) — the aggregator backbone.",
    },
    {
        "name": "greenhouse",
        "source_type": "ats",
        "description": "Greenhouse public boards API, per target employer.",
    },
    {
        "name": "crawl",
        "source_type": "crawl",
        "description": "Polite single-board crawler (robots + rate limit) with constrained Haiku extraction.",
    },
    {
        "name": "jooble",
        "source_type": "api",
        "description": "Jooble aggregator — free-string RoI location coverage (default on).",
    },
    {
        "name": "careerjet",
        "source_type": "api",
        "description": "Careerjet aggregator — Ireland site via en_IE locale (default on).",
    },
    {
        "name": "jsearch",
        "source_type": "api",
        "description": "JSearch/Google-for-Jobs (Indeed/LinkedIn reach) — metered, default OFF.",
    },
]
