"""Phase 2 tests — Scout, connectors, search config, dedupe.

Layered so CI (no external keys) still gets real coverage:
- pure unit: dedupe/normalize + crawler robots politeness (no network/LLM),
- ``config_service``: config-mcp CRUD + isolation (needs DB only),
- ``sources_stack``: live Adzuna/Greenhouse + Scout run tracking + Celery task
  (skips unless a real Anthropic key + Adzuna creds are present).
"""

from __future__ import annotations

import asyncio
import json
import uuid

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.scout import (
    canonicalize_url,
    completeness_rank,
    compute_dedupe_hash,
    normalize_raw,
    record_precedence,
    source_rank,
    unique_contribution,
)
from jobpilot_core.security import hash_password, mint_mcp_context_token
from jobpilot_core.settings import get_settings
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

S = get_settings()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _mk_user() -> uuid.UUID:
    async with session_scope() as s:
        u = await repositories.create_user(s, f"t_{uuid.uuid4().hex}@ex.com", hash_password("x"))
        return u.id


async def _cleanup(uid: uuid.UUID) -> None:
    async with session_scope() as s:
        await repositories.delete_user_cascade(s, uid)


def _run(coro):
    """Run a coroutine on a fresh loop AND a fresh async engine, so repeated
    asyncio.run() calls in a sync test never reuse an engine bound to a dead loop."""
    import jobpilot_core.db as _db

    _db._engine = None
    _db._sessionmaker = None
    return asyncio.run(coro)


def _data(res):
    sc = res.structuredContent
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text) if res.content else None


async def _call(url, uid, tool, args=None):
    headers = {"Authorization": f"Bearer {mint_mcp_context_token(uid, S)}"}
    async with streamablehttp_client(url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return _data(await session.call_tool(tool, args or {}))


# --------------------------------------------------------------------------- #
# Pure unit — dedupe + normalize (no services)
# --------------------------------------------------------------------------- #
def test_canonicalize_url_strips_query_and_slash():
    a = canonicalize_url("https://Boards.Greenhouse.io/stripe/jobs/123/?utm=x#top")
    assert a == "https://boards.greenhouse.io/stripe/jobs/123"


def test_cross_source_dedupe_hash_collapses_same_job():
    # Same job from two connectors: different URLs + different location spelling.
    adz = normalize_raw({"connector": "adzuna", "source": "adzuna", "source_type": "api",
                         "title": "Backend Engineer", "company": "Stripe",
                         "location": "Dublin, Ireland", "url": "https://adzuna/x"})
    gh = normalize_raw({"connector": "greenhouse:stripe", "source": "greenhouse:stripe",
                        "source_type": "ats", "title": "Backend Engineer", "company": "Stripe",
                        "location": "Dublin", "url": "https://boards.greenhouse.io/stripe/y"})
    assert adz.dedupe_hash == gh.dedupe_hash
    # A different role at the same company must NOT collapse.
    other = normalize_raw({"connector": "adzuna", "source": "adzuna", "source_type": "api",
                          "title": "Frontend Engineer", "company": "Stripe", "location": "Dublin"})
    assert other.dedupe_hash != adz.dedupe_hash


def test_dedupe_hash_is_deterministic():
    args = ("Stripe", "Backend Engineer", "Dublin, Ireland", "https://x/1")
    assert compute_dedupe_hash(*args) == compute_dedupe_hash(*args)


def test_source_precedence_ordering():
    # Direct sources outrank aggregators: ats > crawl > aggregator(api).
    assert source_rank("ats") > source_rank("crawl") > source_rank("api")


def test_completeness_tiebreaker_precedence():
    """Addendum 2.2 (revised) step 1: within a source tier, `full` beats `snippet`; the
    source tier still dominates completeness (a full aggregator never outranks an ATS)."""
    assert completeness_rank("full") > completeness_rank("snippet")
    # Same tier (both api): full-text JSearch outranks a Jooble snippet.
    assert record_precedence("api", "full") > record_precedence("api", "snippet")
    # Tier dominates: an ATS snippet still outranks a full-text aggregator.
    assert record_precedence("ats", "snippet") > record_precedence("api", "full")


def test_unique_contribution_counts_source_only_jobs():
    """Step 5: a dedupe_hash produced by exactly one connector is that connector's unique
    contribution; a hash carried by two or more is shared and counts for none."""
    raw = [
        {"connector": "jsearch"},   # hash A — jsearch only
        {"connector": "jooble"},    # hash B — shared with careerjet
        {"connector": "careerjet"}, # hash B — shared
        {"connector": "jooble"},    # hash C — jooble only
    ]
    hashes = ["A", "B", "B", "C"]
    contrib = unique_contribution(raw, hashes)
    assert contrib == {"jsearch": 1, "jooble": 1}  # careerjet added nothing unique


async def test_source_precedence_direct_beats_aggregator(mcp_services):
    """A job in an aggregator AND a direct source collapses to one row keeping the
    direct record, regardless of run order (uses db-mcp; no external keys)."""
    db = mcp_services["db"]

    def job(connector, source_type, url):
        return normalize_raw({"connector": connector, "source": connector,
                              "source_type": source_type, "company": "Stripe",
                              "title": "Backend Engineer", "location": "Dublin",
                              "url": url}).model_dump()

    uid = await _mk_user()
    try:
        # aggregator first, then the direct (ats) copy -> upgrade to direct
        await _call(db, uid, "upsert_jobs", {"jobs": [job("jooble", "api", "https://jooble/x")]})
        await _call(db, uid, "upsert_jobs",
                    {"jobs": [job("greenhouse:stripe", "ats", "https://boards.greenhouse.io/stripe/y")]})
        rows = (await _call(db, uid, "get_jobs", {"limit": 100}))["jobs"]
        assert len(rows) == 1 and rows[0]["source_type"] == "ats"
        assert "greenhouse.io" in rows[0]["url"]
        # a later aggregator copy must NOT clobber the direct record
        await _call(db, uid, "upsert_jobs", {"jobs": [job("careerjet", "api", "https://careerjet/z")]})
        rows = (await _call(db, uid, "get_jobs", {"limit": 100}))["jobs"]
        assert len(rows) == 1 and rows[0]["source_type"] == "ats" and "greenhouse.io" in rows[0]["url"]
    finally:
        await _cleanup(uid)


# --------------------------------------------------------------------------- #
# Pure unit — crawler politeness (robots), no network/LLM
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


class _FakeClient:
    def __init__(self, robots_text):
        self._t = robots_text

    async def get(self, url):
        return _FakeResp(200, self._t)


async def test_crawler_honors_robots_disallow():
    from sources_mcp.connectors.crawl import _robots_ok

    disallow = "User-agent: *\nDisallow: /jobs/\n"
    assert await _robots_ok(_FakeClient(disallow), "https://x.ie/jobs/dev", "Mozilla/5.0") is False
    allow = "User-agent: *\nDisallow: /private/\n"
    assert await _robots_ok(_FakeClient(allow), "https://x.ie/jobs/dev", "Mozilla/5.0") is True


# --------------------------------------------------------------------------- #
# Pure unit — crawler posting-link extraction + JSON-LD parse (no network/LLM)
# --------------------------------------------------------------------------- #
def test_crawler_extracts_posting_links():
    """Posting URLs are pulled straight from /job/ links, absolutized, de-duped, and
    apply/similar sub-paths are excluded."""
    from sources_mcp.connectors.crawl import _extract_posting_links

    base = "https://www.irishjobs.ie/jobs/software-developer/in-dublin"
    page = """
      <a href="/job/software-engineer/acme-job101">A</a>
      <a href="/job/software-engineer/acme-job101?src=x">dup with query</a>
      <a href="https://www.irishjobs.ie/job/data-eng/globex-job202">B</a>
      <a href="/job/software-engineer/acme-job101/apply">apply (excluded)</a>
      <a href="/jobs/software-developer/in-cork">search (excluded)</a>
      <a href="/careeradvice/cv-tips">advice (excluded)</a>
    """
    links = _extract_posting_links(page, base)
    assert links == [
        "https://www.irishjobs.ie/job/software-engineer/acme-job101",
        "https://www.irishjobs.ie/job/data-eng/globex-job202",
    ]


def test_crawler_parses_jobposting_jsonld():
    """The full description + fields come from the schema.org JobPosting JSON-LD."""
    from sources_mcp.connectors.crawl import _parse_jobposting_jsonld

    page = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting",
     "title":"Senior Backend Engineer",
     "description":"<p>Build <b>APIs</b> in Python.</p><ul><li>5+ years</li></ul>",
     "hiringOrganization":{"@type":"Organization","name":"Acme Ltd"},
     "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
        "addressLocality":"Dublin, County Dublin","addressRegion":"Dublin",
        "addressCountry":"IE"}},
     "datePosted":"2026-07-01"}
    </script></head><body>chrome</body></html>
    """
    data = _parse_jobposting_jsonld(page)
    assert data is not None
    assert data["title"] == "Senior Backend Engineer"
    assert data["company"] == "Acme Ltd"
    # Comma-token dedup: "Dublin, County Dublin" + region "Dublin" -> no repeat.
    assert data["location"] == "Dublin, County Dublin, IE"
    assert data["date_posted"] == "2026-07-01"
    # HTML stripped to clean readable text, full body preserved.
    assert data["description"] == "Build APIs in Python. 5+ years"


def test_crawler_jsonld_absent_returns_none():
    from sources_mcp.connectors.crawl import _parse_jobposting_jsonld

    assert _parse_jobposting_jsonld("<html><body>no structured data</body></html>") is None


# --------------------------------------------------------------------------- #
# Pure unit — profile-aware search term derivation (Step 1; no network/LLM)
# --------------------------------------------------------------------------- #
def test_derive_search_terms_from_resume():
    from jobpilot_core.profile_search import derive_search_terms

    resume = {
        "contact": {"location": "Dublin, Ireland"},
        "skills": ["Python", "Kubernetes", "AWS", "Python"],  # dupe collapses
        "experience": [
            {"title": "Senior Software Engineer, Acme Corp (2020-2024)"},
            {"title": "Backend Developer | Globex"},
            {"title": "Managed the on-call rotation"},  # not a title -> dropped
        ],
    }
    d = derive_search_terms(resume)
    assert d["roles"] == ["Senior Software Engineer", "Backend Developer"]
    assert d["keywords"] == ["Python", "Kubernetes", "AWS"]
    assert d["locations"] == ["Dublin, Ireland"]
    assert d["seniority"] == "senior"


def test_derive_search_terms_empty_resume():
    from jobpilot_core.profile_search import derive_search_terms

    d = derive_search_terms(None)
    assert d == {"roles": [], "keywords": [], "locations": [], "seniority": None}


def test_merge_search_terms_config_leads_profile_extends():
    from jobpilot_core.profile_search import merge_search_terms

    config = {"roles": ["Platform Engineer"], "keywords": ["Go"], "locations": ["Cork"]}
    derived = {"roles": ["Backend Developer", "Platform Engineer"], "keywords": ["Go", "Python"]}
    merged = merge_search_terms(config, derived)
    assert merged["roles"] == ["Platform Engineer", "Backend Developer"]  # config first, deduped
    assert merged["keywords"] == ["Go", "Python"]
    assert merged["locations"] == ["Cork"]  # explicit config location kept, not diluted


# --------------------------------------------------------------------------- #
# Pure unit — Greenhouse slug harvest + board merge (Step 2; no network)
# --------------------------------------------------------------------------- #
def test_extract_greenhouse_slugs():
    from jobpilot_core.ats_boards import extract_greenhouse_slugs

    urls = [
        "https://boards.greenhouse.io/intercom/jobs/12345",
        "https://job-boards.greenhouse.io/stripe/jobs/999",
        "https://boards.greenhouse.io/embed/job_board?for=datadog&token=1",
        "https://www.adzuna.co.uk/jobs/land/ad/123",  # not greenhouse -> ignored
        None,
    ]
    assert extract_greenhouse_slugs(urls) == {"intercom", "stripe", "datadog"}


def test_merge_boards_unions_and_caps():
    from jobpilot_core.ats_boards import DEFAULT_GREENHOUSE_BOARDS, merge_boards

    boards = merge_boards(["MyCo", "intercom"], {"newco", "stripe"}, cap=40)
    assert boards[0] == "myco"  # configured first, lowercased
    assert "intercom" in boards and "stripe" in boards and "newco" in boards
    assert len(boards) == len(set(boards))  # de-duped
    # Defaults are folded in.
    assert set(DEFAULT_GREENHOUSE_BOARDS).issubset(set(boards))
    # Cap is honored.
    assert len(merge_boards([f"c{i}" for i in range(100)], set(), cap=10)) == 10


# --------------------------------------------------------------------------- #
# Addendum 2.2 — description completeness tagging (no network/LLM)
# --------------------------------------------------------------------------- #
def test_connector_tags_description_completeness():
    """Connectors set the source-appropriate completeness, and normalize_raw carries
    it through to the persisted shape."""
    from sources_mcp.connectors.adzuna import _to_raw as adzuna_raw
    from sources_mcp.connectors.careerjet import _to_raw as careerjet_raw
    from sources_mcp.connectors.jooble import _to_raw as jooble_raw
    from sources_mcp.connectors.jsearch import _to_raw as jsearch_raw

    assert jooble_raw({"title": "x", "snippet": "t"}).description_completeness == "snippet"
    assert careerjet_raw({"title": "x", "description": "t"}).description_completeness == "snippet"
    assert adzuna_raw({"title": "x"}).description_completeness == "snippet"
    assert jsearch_raw({"job_title": "x"}).description_completeness == "full"

    ji = normalize_raw(jooble_raw({"title": "x", "company": "c", "snippet": "t"}))
    assert ji.description_completeness == "snippet"


async def test_completeness_persisted_and_precedence(mcp_services):
    """The completeness flag round-trips through upsert/get, and an ATS full-text twin
    overwrites an aggregator snippet's flag when it collapses onto the same row."""
    db = mcp_services["db"]

    def job(connector, source_type, completeness, url):
        return normalize_raw({
            "connector": connector, "source": connector, "source_type": source_type,
            "company": "Globex", "title": "Data Engineer", "location": "Dublin",
            "url": url, "description": "text", "description_completeness": completeness,
        }).model_dump()

    uid = await _mk_user()
    try:
        await _call(db, uid, "upsert_jobs",
                    {"jobs": [job("jooble", "api", "snippet", "https://jooble/x")]})
        rows = (await _call(db, uid, "get_jobs", {"limit": 100}))["jobs"]
        assert len(rows) == 1 and rows[0]["description_completeness"] == "snippet"
        # the ATS twin (direct) collapses on and upgrades the flag to full
        await _call(db, uid, "upsert_jobs",
                    {"jobs": [job("greenhouse:globex", "ats", "full",
                                  "https://boards.greenhouse.io/globex/y")]})
        rows = (await _call(db, uid, "get_jobs", {"limit": 100}))["jobs"]
        assert len(rows) == 1 and rows[0]["description_completeness"] == "full"
    finally:
        await _cleanup(uid)


async def test_full_text_beats_snippet_within_tier(mcp_services):
    """Addendum 2.2 (revised) step 1: the SAME role arriving full-text (JSearch) and as
    a Jooble snippet — both source_type='api' — collapses to the full-text record and is
    marked `full`, regardless of connector run order (snippet never wins the contest)."""
    db = mcp_services["db"]

    def job(connector, completeness, url, description):
        return normalize_raw({
            "connector": connector, "source": connector, "source_type": "api",
            "company": "Initech", "title": "Platform Engineer", "location": "Dublin",
            "url": url, "description": description, "description_completeness": completeness,
        }).model_dump()

    for order in (
        # snippet first, then full — full must upgrade the row
        [("jooble", "snippet", "https://jooble/x", "teaser"),
         ("jsearch", "full", "https://jsearch/y", "full description text")],
        # full first, then snippet — snippet must NOT clobber the full record
        [("jsearch", "full", "https://jsearch/y", "full description text"),
         ("jooble", "snippet", "https://jooble/x", "teaser")],
    ):
        uid = await _mk_user()
        try:
            for connector, completeness, url, desc in order:
                await _call(db, uid, "upsert_jobs", {"jobs": [job(connector, completeness, url, desc)]})
            rows = (await _call(db, uid, "get_jobs", {"limit": 100}))["jobs"]
            assert len(rows) == 1
            assert rows[0]["description_completeness"] == "full"
            assert rows[0]["connector"] == "jsearch"  # the full-text survivor
            assert rows[0]["description"] == "full description text"
        finally:
            await _cleanup(uid)


# --------------------------------------------------------------------------- #
# config-mcp CRUD + isolation (DB only; runs in CI)
# --------------------------------------------------------------------------- #
async def test_search_config_crud_and_isolation(config_service):
    a, b = await _mk_user(), await _mk_user()
    try:
        assert (await _call(config_service, a, "get_search_config"))["config"]["roles"] == []
        upd = await _call(config_service, a, "update_search_config",
                          {"roles": ["Backend Engineer"], "sources": ["adzuna", "greenhouse"],
                           "target_employers": ["stripe"]})
        assert upd["config"]["sources"] == ["adzuna", "greenhouse"]
        # partial update keeps roles
        await _call(config_service, a, "update_search_config", {"keywords": ["python"]})
        after = await _call(config_service, a, "get_search_config")
        assert after["config"]["roles"] == ["Backend Engineer"]
        assert after["config"]["keywords"] == ["python"]
        # user B cannot see A's config
        assert (await _call(config_service, b, "get_search_config"))["config"]["roles"] == []
    finally:
        await _cleanup(a)
        await _cleanup(b)


# --------------------------------------------------------------------------- #
# Live connectors + Scout run tracking (skips without keys)
# --------------------------------------------------------------------------- #
async def test_scout_adzuna_and_greenhouse_persist_idempotent(sources_stack):
    from app import mcp_client
    from app.scout_runner import run_scout

    uid = await _mk_user()
    try:
        await mcp_client.config_mcp(uid, "update_search_config", {
            "roles": ["Software Engineer"], "locations": ["Dublin", "Belfast"],
            "keywords": ["python"], "sources": ["adzuna", "greenhouse"],
            "target_employers": ["stripe"]})
        r1 = await run_scout(uid)
        assert r1["errors"] == {}
        assert r1["by_connector"].get("adzuna", 0) > 0
        assert r1["by_connector"].get("greenhouse:stripe", 0) > 0
        assert r1["new"] == r1["unique"] > 0

        jobs = (await mcp_client.db_mcp(uid, "get_jobs", {"limit": 5000}))["jobs"]
        types = {j["source_type"] for j in jobs}
        assert {"api", "ats"} <= types
        assert len({j["dedupe_hash"] for j in jobs}) == len(jobs)  # no dup rows

        r2 = await run_scout(uid)  # idempotent
        assert r2["new"] == 0
        assert len((await mcp_client.db_mcp(uid, "get_jobs", {"limit": 5000}))["jobs"]) == len(jobs)
    finally:
        await _cleanup(uid)


async def test_scout_graceful_degradation_and_run_tracking(sources_stack):
    from app import mcp_client
    from app.scout_runner import run_scout_tracked

    uid = await _mk_user()
    try:
        await mcp_client.config_mcp(uid, "update_search_config", {
            "roles": ["Software Engineer"], "locations": ["Belfast"], "keywords": ["python"],
            "sources": ["adzuna", "greenhouse"], "target_employers": ["no-such-board-xyz-123"]})
        summary = await run_scout_tracked(uid)
        assert "greenhouse:no-such-board-xyz-123" in summary["errors"]  # recorded
        assert summary["by_connector"].get("adzuna", 0) > 0  # good connector still ran

        runs = (await mcp_client.db_mcp(uid, "get_pipeline_runs", {"stage": "scout"}))["runs"]
        assert len(runs) == 1 and runs[0]["status"] == "completed"  # not fatal
        assert runs[0]["started_at"] and runs[0]["finished_at"]
        assert runs[0]["counts"]["new"] == summary["new"]
        assert "no-such-board-xyz-123" in runs[0]["error"]
    finally:
        await _cleanup(uid)


def test_celery_scout_task_runs_and_tracks(sources_stack):
    """Sync test: the Celery task executes Scout end-to-end via .apply() (no broker)."""
    from app import mcp_client
    from app.tasks import scout_run

    uid = _run(_mk_user())
    try:
        _run(mcp_client.config_mcp(uid, "update_search_config", {
            "roles": ["Software Engineer"], "locations": ["Belfast"], "keywords": ["python"],
            "sources": ["adzuna"]}))
        result = scout_run.apply(args=[str(uid)])
        summary = result.get()
        assert summary["new"] > 0 and "run_id" in summary

        runs = _run(mcp_client.db_mcp(uid, "get_pipeline_runs", {"stage": "scout"}))["runs"]
        assert len(runs) == 1 and runs[0]["status"] == "completed"
    finally:
        _run(_cleanup(uid))
