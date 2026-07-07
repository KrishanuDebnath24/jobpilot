"""Shared Scout tail: the one raw job shape every connector emits, plus the
deterministic normalize + dedupe step that consumes all of them.

Adding a source later is a new connector, not a new pipeline — connectors return
:class:`RawJob`, and :func:`normalize_raw` maps any of them into the ``jobs`` shape
(:class:`~jobpilot_core.schemas.JobIn`). No LLM here: reshaping already-structured
connector output is deterministic field-mapping by design (the cost trap to avoid).
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

from .schemas import JobIn

_SOURCE_TYPES = {"api", "ats", "crawl", "seed"}

# Source precedence for dedupe collapse: a job carried by both a *direct* source and
# an *aggregator* keeps the direct record (canonical apply link, richer fields).
# Order: ats (Greenhouse/company) > crawl (board) > aggregator (Adzuna/Jooble/
# Careerjet/JSearch, all source_type='api'). Aggregators fill coverage gaps; they
# never overwrite direct data.
_SOURCE_RANK = {"ats": 3, "crawl": 2, "api": 1, "seed": 0}

# Addendum 2.2 (revised) — full-text-primary. WITHIN a source tier, a `full` record
# beats a `snippet` one for the same dedupe_hash, so a snippet never wins a contest
# against full text (e.g. JSearch full-text vs a Jooble snippet, both source_type
# 'api'). This is the *secondary* precedence key: source tier decides first, then
# completeness. Snippets only ever add coverage; they never overwrite full text.
_COMPLETENESS_RANK = {"full": 1, "snippet": 0}


def source_rank(source_type: str | None) -> int:
    return _SOURCE_RANK.get(source_type or "", 1)


def completeness_rank(completeness: str | None) -> int:
    return _COMPLETENESS_RANK.get(completeness or "", 0)


def record_precedence(source_type: str | None, completeness: str | None) -> tuple[int, int]:
    """Combined dedupe precedence for a record: (source tier, completeness). Compared
    as a tuple so tier dominates and `full` breaks ties within a tier. A higher tuple
    wins the collapse for a shared ``dedupe_hash``."""
    return (source_rank(source_type), completeness_rank(completeness))


class RawJob(BaseModel):
    """The common raw shape returned by every connector (adzuna / greenhouse / crawl)."""

    connector: str  # e.g. "adzuna", "greenhouse:intercom", "crawl:irishjobs"
    source: str  # human-facing feed name
    source_type: str  # api | ats | crawl
    title: str | None = None
    company: str | None = None
    location: str | None = None
    description: str | None = None
    url: str | None = None
    external_id: str | None = None
    # Addendum 2.2: how complete `description` is. Direct sources (ATS/crawl/JSearch)
    # emit "full"; aggregators (Adzuna/Jooble/Careerjet) emit "snippet" teaser text.
    description_completeness: str = "snippet"
    raw: dict = Field(default_factory=dict)  # the connector's original payload


def _norm(text: str | None) -> str:
    """Lowercase, collapse whitespace, drop surrounding punctuation."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .,-|")


def _coarse_location(location: str | None) -> str:
    """First comma-delimited token, normalized. 'Dublin, County Dublin' and
    'Dublin, Ireland' both collapse to 'dublin' so the *same* job from two sources
    dedupes even when each feed spells the location differently."""
    if not location:
        return ""
    return _norm(location.split(",")[0])


def canonicalize_url(url: str | None) -> str | None:
    """Strip query/fragment + trailing slash and lowercase scheme+host so the same
    posting via different tracking URLs canonicalizes consistently."""
    if not url:
        return None
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url
    if not p.scheme or not p.netloc:
        return url
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))


def compute_dedupe_hash(
    company: str | None, title: str | None, location: str | None, url: str | None = None
) -> str:
    """Primary key = normalized company + title + coarse location, so a job carried
    by *both* Adzuna and an ATS collapses to one row (their URLs differ, so URL is
    deliberately NOT in the key). URL is a fallback only when company+title are absent
    (some crawled listings)."""
    company_n, title_n, loc_n = _norm(company), _norm(title), _coarse_location(location)
    if not company_n and not title_n:
        basis = canonicalize_url(url) or ""
    else:
        basis = f"{company_n}|{title_n}|{loc_n}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def normalize_raw(raw: RawJob | dict) -> JobIn:
    """Deterministically map a raw connector job into the ``jobs`` table shape,
    computing the dedupe hash. Idempotent: same input -> same dedupe_hash."""
    rj = raw if isinstance(raw, RawJob) else RawJob.model_validate(raw)
    source_type = rj.source_type if rj.source_type in _SOURCE_TYPES else "crawl"
    url = canonicalize_url(rj.url)
    normalized = {
        "title": rj.title,
        "company": rj.company,
        "location": rj.location,
        "url": url,
        "source": rj.source,
        "source_type": source_type,
        "connector": rj.connector,
        "external_id": rj.external_id,
        "description_completeness": rj.description_completeness,
    }
    return JobIn(
        title=rj.title,
        company=rj.company,
        location=rj.location,
        description=rj.description,
        url=url,
        source=rj.source,
        source_type=source_type,
        connector=rj.connector,
        description_completeness=rj.description_completeness,
        dedupe_hash=compute_dedupe_hash(rj.company, rj.title, rj.location, url),
        raw_json=rj.raw or None,
        normalized_json=normalized,
        status="scouted",
    )


def unique_contribution(
    raw_jobs: list[RawJob | dict], hashes: list[str]
) -> dict[str, int]:
    """Per-connector count of deduped jobs seen *only* via that connector in this batch
    (Addendum 2.2 revised, step 5 — the measured drop-candidate signal).

    ``raw_jobs`` and ``hashes`` are parallel: ``hashes[i]`` is the ``dedupe_hash`` of
    ``raw_jobs[i]``. A ``dedupe_hash`` produced by exactly one connector counts as that
    connector's unique contribution; a hash carried by two or more is shared and counts
    for none. This measures *unique breadth after dedup*, so a source is judged on what
    it adds that nothing else does — not on the "snippet" label.
    """
    by_hash: dict[str, set[str]] = {}
    for rj, h in zip(raw_jobs, hashes):
        connector = rj.get("connector") if isinstance(rj, dict) else rj.connector
        by_hash.setdefault(h, set()).add(connector or "?")
    out: dict[str, int] = {}
    for connectors in by_hash.values():
        if len(connectors) == 1:
            (only,) = tuple(connectors)
            out[only] = out.get(only, 0) + 1
    return out
