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
# `hydrated` (Level 2 borrowed full text) sits between: it outranks a snippet — so a
# re-scraped teaser can't clobber a job we already hydrated — but genuine full text
# still upgrades it.
_COMPLETENESS_RANK = {"full": 2, "hydrated": 1, "snippet": 0}


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


# --------------------------------------------------------------------------- #
# Cross-source hydration (Level 1): canonicalize company + title so the SAME job
# carried by an aggregator (snippet) AND a direct source (full) collapses to one
# dedupe_hash. The higher-precedence full-text record then wins in `upsert_jobs`
# and overwrites the snippet body — a snippet is hydrated to full text for free.
#
# DELIBERATELY CONSERVATIVE: we strip only *non-functional* decorations — legal
# suffixes, employment mode, gender markers — never functional qualifiers like
# "Frontend"/"Backend"/a team name. So two genuinely different roles at the same
# employer never merge (that is the false-merge guard pinned down in the tests).
# --------------------------------------------------------------------------- #

# Legal-entity suffixes dropped from a company name so "Stripe" == "Stripe, Inc." ==
# "Stripe Ltd". Excludes "group"/"holdings" — those can denote distinct legal entities.
_COMPANY_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "llc", "llp", "lp", "plc", "gmbh", "ag", "sa", "sas", "srl", "bv", "nv", "oy",
    "ab", "as", "pty",
}

# Non-functional title decorations: employment mode + contract type. Stripped only when
# a whole parenthetical or a standalone trailing ", X" / " - X" segment. Functional words
# (frontend, data, security, a product/team name) are intentionally NOT here, so
# "Engineer, Frontend" and "Engineer, Backend" stay distinct.
_TITLE_DECORATIONS = {
    "remote", "hybrid", "onsite", "on-site", "on site", "wfh",
    "contract", "contractor", "permanent", "perm", "temporary", "temp",
    "full-time", "fulltime", "full time", "part-time", "parttime", "part time",
    "fte", "freelance", "fixed-term", "fixed term",
}

# Gender/diversity markers common in EU postings: (m/f/d), f/m/x, (w/m/d), m/f, ...
_GENDER_MARKER_RE = re.compile(r"\b[mwfdx](?:\s*/\s*[mwfdx]){1,2}\b")


def _canon_company(company: str | None) -> str:
    """Normalize a company name for dedupe: drop a trailing '(ACRONYM)', a leading
    'the', and trailing legal suffixes, so the same employer spelled differently across
    sources collapses. Falls back to plain `_norm` if canonicalization empties it."""
    base = _norm(company)
    if not base:
        return ""
    base = re.sub(r"\s*\([^)]*\)\s*$", "", base).strip()  # trailing "(msc)" acronym
    stripped = re.sub(r"^the\s+", "", base.replace("&", " and "))
    tokens = re.split(r"[\s,]+", re.sub(r"\s+", " ", stripped).strip())
    while len(tokens) > 1 and re.sub(r"[.\-]", "", tokens[-1]) in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens).strip() or base


def _decoration_only(segment: str) -> bool:
    """True if a title segment is purely non-functional decoration (employment mode,
    contract type, or a gender marker) — hence safe to strip without merging roles."""
    s = _GENDER_MARKER_RE.sub("", segment).strip(" -,/|")
    s = re.sub(r"\s+", " ", s)
    return not s or s in _TITLE_DECORATIONS


def _canon_title(title: str | None) -> str:
    """Normalize a job title for dedupe by removing ONLY non-functional decorations:
    parenthetical mode/gender tags and trailing ', <mode>' / ' - <mode>' segments.
    Functional qualifiers (Frontend, Data, a team/product name) are preserved so
    distinct roles never collapse. Falls back to `_norm` if canonicalization empties it."""
    base = _norm(title)
    if not base:
        return ""
    t = re.sub(r"[–—]", "-", base)  # en/em dash -> hyphen
    t = re.sub(r"[\(\[]([^)\]]*)[\)\]]",  # drop decoration-only parentheticals
               lambda m: " " if _decoration_only(m.group(1)) else m.group(0), t)
    t = _GENDER_MARKER_RE.sub(" ", t)  # drop free-standing gender markers
    t = re.sub(r"\s+", " ", t).strip()
    # Peel trailing decoration-only segments (", remote", " - full time"). Splitting on
    # ", " / " - " keeps hyphenated roles ("Full-Stack Engineer") intact (no surrounding
    # spaces around that hyphen) while catching " Engineer - Remote".
    parts = [p.strip() for p in re.split(r"\s*,\s*|\s+-\s+", t) if p.strip()]
    while len(parts) > 1 and _decoration_only(parts[-1]):
        parts.pop()
    return ", ".join(parts) or base


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
    """Primary key = canonicalized company + title + coarse location, so a job carried
    by *both* an aggregator and an ATS collapses to one row (their URLs differ, so URL
    is deliberately NOT in the key). Company/title are canonicalized (legal suffixes,
    employment-mode/gender decorations dropped — see `_canon_company`/`_canon_title`) so
    cross-source spelling differences still collapse and the full-text record hydrates
    the snippet. URL is a fallback only when company+title are absent (some crawls)."""
    company_n, title_n, loc_n = _canon_company(company), _canon_title(title), _coarse_location(location)
    if not company_n and not title_n:
        basis = canonicalize_url(url) or ""
    else:
        basis = f"{company_n}|{title_n}|{loc_n}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Employment-type + seniority classification (deterministic, NO LLM — same principle
# as the rest of the normalize step). Structured connector fields decide first (the
# aggregators already report them); a title-keyword heuristic is the fallback so
# crawler/ATS rows that carry no such field are still classified. Both are best-effort
# and nullable: an unknown value stays None rather than being guessed.
# --------------------------------------------------------------------------- #

# Normalized vocabularies (seniority mirrors the Analyzer's enum for cross-stage consistency).
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "temporary", "internship")
SENIORITY_LEVELS = (
    "intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager", "director",
)

# JSearch `job_employment_type` (uppercased) -> normalized employment type.
_JSEARCH_EMPLOYMENT = {
    "FULLTIME": "full_time", "PARTTIME": "part_time", "CONTRACTOR": "contract",
    "CONTRACT": "contract", "INTERN": "internship", "INTERNSHIP": "internship",
    "TEMPORARY": "temporary", "TEMP": "temporary",
}

# The Muse `levels` name (lowercased) -> normalized seniority.
_MUSE_LEVEL_SENIORITY = {
    "internship": "intern", "entry level": "junior", "mid level": "mid",
    "senior level": "senior", "management": "manager",
}

# Title-keyword rules: first matching row wins, so order encodes precedence.
_EMPLOYMENT_TITLE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("internship", ("internship", "intern")),
    ("contract", ("contractor", "contract", "fixed term", "fixed-term", "freelance")),
    ("part_time", ("part-time", "part time")),
    ("temporary", ("temporary", "temp")),
)
_SENIORITY_TITLE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("director", ("director", "vp", "vice president", "head of", "chief", "cto", "ceo", "cfo")),
    ("principal", ("principal",)),
    ("staff", ("staff",)),
    ("lead", ("lead",)),
    ("manager", ("manager", "management")),
    ("senior", ("senior", "sr")),
    ("junior", ("junior", "jr", "graduate", "trainee", "entry level", "entry-level")),
    ("intern", ("intern", "internship")),
)


def _has_kw(text: str, kw: str) -> bool:
    """Word-boundary-ish presence of ``kw`` in already-normalized ``text`` — so 'intern'
    matches the standalone word but NOT 'internal', and 'contract' does not fire on
    'contractor' (which is listed separately). Same matcher family as the qualifier gate."""
    return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None


def _match_title_rules(title_norm: str, rules: tuple[tuple[str, tuple[str, ...]], ...]) -> str | None:
    for value, keywords in rules:
        if any(_has_kw(title_norm, kw) for kw in keywords):
            return value
    return None


def _employment_from_raw(raw: dict) -> str | None:
    """Employment type from a connector's structured fields: JSearch `job_employment_type`,
    then Adzuna `contract_type` (permanent|contract) + `contract_time` (full_time|part_time)."""
    jt = raw.get("job_employment_type")
    if isinstance(jt, str):
        mapped = _JSEARCH_EMPLOYMENT.get(jt.strip().upper())
        if mapped:
            return mapped
    ctype = str(raw.get("contract_type") or "").strip().lower()
    if ctype == "contract":
        return "contract"
    ctime = str(raw.get("contract_time") or "").strip().lower().replace("-", "_")
    if ctime == "part_time":
        return "part_time"
    if ctime == "full_time" or ctype == "permanent":
        return "full_time"
    return None


def _seniority_from_raw(raw: dict) -> str | None:
    """Seniority from The Muse's `levels` list (the one connector that reports it structurally)."""
    levels = raw.get("levels")
    if isinstance(levels, list):
        for lv in levels:
            mapped = _MUSE_LEVEL_SENIORITY.get(str(lv or "").strip().lower())
            if mapped:
                return mapped
    return None


def classify_job(title: str | None, raw: dict | None) -> tuple[str | None, str | None]:
    """Return ``(employment_type, seniority)`` for a job — structured connector fields
    first, title-keyword heuristic as the fallback. Deterministic, no LLM; either value is
    None when nothing reliable is found (never guessed)."""
    raw = raw or {}
    title_norm = _norm(title)
    employment = _employment_from_raw(raw) or _match_title_rules(title_norm, _EMPLOYMENT_TITLE_RULES)
    seniority = _seniority_from_raw(raw) or _match_title_rules(title_norm, _SENIORITY_TITLE_RULES)
    return employment, seniority


def normalize_raw(raw: RawJob | dict) -> JobIn:
    """Deterministically map a raw connector job into the ``jobs`` table shape,
    computing the dedupe hash. Idempotent: same input -> same dedupe_hash."""
    rj = raw if isinstance(raw, RawJob) else RawJob.model_validate(raw)
    source_type = rj.source_type if rj.source_type in _SOURCE_TYPES else "crawl"
    url = canonicalize_url(rj.url)
    employment_type, seniority = classify_job(rj.title, rj.raw)
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
        "employment_type": employment_type,
        "seniority": seniority,
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
        employment_type=employment_type,
        seniority=seniority,
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


# --------------------------------------------------------------------------- #
# Cross-source hydration (Level 2): borrow a full-text body for a `snippet` job that is
# the SAME posting as a `full`/`hydrated` job which Level 1's hash did NOT collapse
# (e.g. an aggregator title carries a team suffix the ATS title lacks). Unlike Level 1
# this does NOT merge rows — it copies the description onto the snippet and marks it
# `hydrated`. Deterministic, no LLM. Tuned to "only if extremely sure": a confident
# same-role rule + hard anchors (same canonical company AND coarse location) + a
# uniqueness guard (refuse if more than one full candidate could match).
# --------------------------------------------------------------------------- #

# Tokens that CHANGE a role's discipline or level. If the token-set *difference* between
# two titles contains any of these, they are different roles and we refuse to hydrate —
# this is the safety core. (Shared functional tokens are fine; only the diff is judged.)
_FUNCTIONAL_TOKENS = {
    # disciplines / stacks
    "frontend", "front-end", "backend", "back-end", "fullstack", "full-stack",
    "mobile", "ios", "android", "web", "cloud", "data", "database", "analytics",
    "ml", "ai", "nlp", "security", "infosec", "platform", "infrastructure", "infra",
    "devops", "sre", "reliability", "qa", "quality", "test", "testing", "automation",
    "embedded", "firmware", "systems", "network", "networking", "hardware", "software",
    # functions
    "sales", "marketing", "finance", "hr", "legal", "design", "product", "research",
    "support", "operations", "ops", "growth", "recruiting", "people", "accounting",
    # languages / frameworks
    "python", "java", "javascript", "typescript", "golang", "go", "rust", "ruby",
    "scala", "kotlin", "swift", "php", "node", "react", "angular", "vue", "dotnet",
    "sql", "cpp",
    # seniority / level
    "intern", "internship", "graduate", "junior", "jr", "mid", "senior", "sr",
    "staff", "principal", "lead", "manager", "director", "head", "vp", "chief",
    "associate", "apprentice",
}


def _title_tokens(title: str | None) -> set[str]:
    return {t for t in re.split(r"[\s,]+", _canon_title(title)) if t}


def _confident_same_role(title_a: str | None, title_b: str | None) -> bool:
    """True only when two titles are confidently the SAME role: after Level 1
    canonicalization, one title's token set contains the other's, the smaller has at
    least two tokens (a one-token title like "engineer" is too generic to be sure), and
    every EXTRA token in the larger is non-functional (a team/product name — not a
    discipline, stack, function, or seniority word). Any functional or seniority
    difference => treated as a different role => not a match."""
    ta, tb = _title_tokens(title_a), _title_tokens(title_b)
    if not ta or not tb:
        return False
    smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(smaller) < 2 or not smaller <= larger:
        return False
    return not (larger - smaller) & _FUNCTIONAL_TOKENS


def plan_hydration(jobs: list[dict]) -> list[dict]:
    """Plan Level 2 hydration over one user's jobs (deterministic, no LLM).

    For each ``snippet`` job, find ``full``/``hydrated`` jobs that are confidently the
    same posting — same canonical company AND same coarse location AND
    :func:`_confident_same_role` title — and, only when EXACTLY ONE such candidate
    exists (more than one is ambiguous → refuse), plan to copy its description onto the
    snippet. Returns ``[{"snippet_id", "source_id", "description"}]``. Refuses on any
    missing company/location anchor, so it never guesses. Each job dict needs ``id``,
    ``company``, ``title``, ``location``, ``description``, ``description_completeness``."""
    fulls = [
        j for j in jobs
        if j.get("description_completeness") in ("full", "hydrated")
        and (j.get("description") or "").strip()
    ]
    plans: list[dict] = []
    for s in jobs:
        if s.get("description_completeness") != "snippet":
            continue
        company, loc = _canon_company(s.get("company")), _coarse_location(s.get("location"))
        if not company or not loc:
            continue  # both anchors required — never hydrate without them
        current = (s.get("description") or "").strip()
        candidates = [
            f for f in fulls
            if f.get("id") != s.get("id")
            and _canon_company(f.get("company")) == company
            and _coarse_location(f.get("location")) == loc
            and _confident_same_role(s.get("title"), f.get("title"))
            and len((f.get("description") or "").strip()) > len(current)
        ]
        if len(candidates) != 1:
            continue  # 0 → nothing to borrow; >1 → ambiguous → refuse (extremely sure)
        src = candidates[0]
        plans.append({
            "snippet_id": s["id"], "source_id": src["id"],
            "description": (src.get("description") or "").strip(),
        })
    return plans
