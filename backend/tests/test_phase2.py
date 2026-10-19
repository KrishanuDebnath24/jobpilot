"""Phase 2 tests — Scout, connectors, search config, dedupe.

Layered so CI (no external keys) still gets real coverage:
- pure unit: dedupe/normalize + crawler robots politeness (no network/LLM),
- ``config_service``: config-mcp CRUD + isolation (needs DB only),
- ``sources_stack``: live Workable/Greenhouse + Scout run tracking + Celery task
  (skips unless a real Anthropic key is present; both connectors are keyless).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.scout import (
    _confident_same_role,
    canonicalize_url,
    classify_job,
    completeness_rank,
    compute_dedupe_hash,
    normalize_raw,
    plan_hydration,
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


# --------------------------------------------------------------------------- #
# Level 1 cross-source hydration — canonicalize company/title so a snippet job
# collapses onto its full-text twin (upsert precedence then hydrates the body).
# --------------------------------------------------------------------------- #
def test_hydration_company_legal_suffix_collapses():
    """Same employer spelled with/without a legal suffix or trailing acronym collapses to
    one dedupe_hash, so a full-text ATS row can overwrite an aggregator snippet."""
    def h(company):
        return compute_dedupe_hash(company, "Backend Engineer", "Dublin")

    assert h("Stripe") == h("Stripe, Inc.") == h("Stripe Ltd") == h("Stripe Limited")
    assert h("Medical Supply Co Ltd (MSC)") == h("Medical Supply Company")  # live-data shape
    assert h("The Hershey Company") == h("Hershey")  # leading "The" + suffix
    assert h("Ben & Jerry's") == h("Ben and Jerry's")  # &/and


def test_canon_company_is_public_and_returns_a_usable_needle():
    """``canon_company`` is no longer dedupe-only — the ``company`` search filter reduces
    the user's phrasing with it before the ILIKE, so the exact string matters, not just
    that two inputs agree. A search for 'Stripe, Inc.' must produce a needle that a row
    stored as plain 'Stripe' contains."""
    from jobpilot_core.scout import canon_company

    assert canon_company("Stripe, Inc.") == "stripe"
    assert canon_company("Stripe") == "stripe"
    assert canon_company("The Hershey Company") == "hershey"
    # Never empty for non-empty input: the filter falls back to it rather than dropping.
    assert canon_company("Ltd") == "ltd"  # a lone suffix is the name, not a suffix
    assert canon_company("") == "" and canon_company(None) == ""


def test_hydration_title_decorations_collapse():
    """Non-functional title decorations (employment mode, contract type, gender markers,
    parentheticals) are stripped so the same role from two feeds collapses."""
    def h(title):
        return compute_dedupe_hash("Acme", title, "Dublin")

    base = h("Backend Engineer")
    assert h("Backend Engineer (Remote)") == base
    assert h("Backend Engineer - Full-time") == base
    assert h("Backend Engineer, Permanent") == base
    assert h("Backend Engineer (m/f/d)") == base
    assert h("Backend Engineer (f/m/x)") == base


def test_hydration_false_merge_guards():
    """Functional qualifiers must NEVER be stripped: distinct roles at the same employer
    keep distinct hashes (no false merge)."""
    def h(title):
        return compute_dedupe_hash("Acme", title, "Dublin")

    # discipline suffix kept -> distinct from each other AND from the bare role
    assert h("Software Engineer, Frontend") != h("Software Engineer, Backend")
    assert h("Software Engineer, Frontend") != h("Software Engineer")
    assert h("Software Engineer (Frontend)") != h("Software Engineer (Backend)")
    # a real functional segment that merely occupies the trailing slot is kept
    assert h("Manager, Business Development") != h("Manager, Sales")
    assert h("Data Engineer") != h("Data Scientist")
    # hyphenated role words survive (no spaces around the hyphen), stay distinct
    assert h("Full-Stack Engineer") != h("Backend Engineer")
    # different companies never collapse
    assert compute_dedupe_hash("Stripe", "Backend Engineer", "Dublin") != \
        compute_dedupe_hash("Square", "Backend Engineer", "Dublin")


def test_hydration_conservative_boundary_team_suffix_kept():
    """DELIBERATE boundary: an arbitrary team/product proper-noun suffix is NOT stripped
    (can't be told apart from ', Frontend' safely), so a ', <team>' aggregator title does
    NOT collapse onto the bare ATS title. That residual is Level 2's job, not Level 1's."""
    with_team = compute_dedupe_hash(
        "State Street", "Senior QA Automation Engineer, Charles River Development", "Dublin")
    bare = compute_dedupe_hash("State Street", "Senior QA Automation Engineer", "Dublin")
    assert with_team != bare


# --------------------------------------------------------------------------- #
# Level 2 cross-source hydration — plan_hydration: borrow a full body for a snippet
# that is confidently the SAME posting, but ONLY when extremely sure.
# --------------------------------------------------------------------------- #
def _snip(id, title, company="State Street", location="Dublin", desc="teaser"):
    return {"id": id, "title": title, "company": company, "location": location,
            "description": desc, "description_completeness": "snippet"}


def _full(id, title, company="State Street", location="Dublin", desc="X" * 2000):
    return {"id": id, "title": title, "company": company, "location": location,
            "description": desc, "description_completeness": "full"}


def test_confident_same_role_rule():
    # team/product suffix -> same role; discipline/seniority diff or 1-token -> not.
    assert _confident_same_role("Senior QA Engineer", "Senior QA Engineer, Charles River Dev")
    assert _confident_same_role("Backend Engineer", "Backend Engineer, Payments Team")
    assert not _confident_same_role("Software Engineer", "Software Engineer, Frontend")
    assert not _confident_same_role("Engineer", "Engineer, Platform")   # 1-token subset
    assert not _confident_same_role("Backend Engineer", "Senior Backend Engineer")  # seniority
    assert not _confident_same_role("Data Engineer", "Data Scientist")  # not containment


def test_hydration_pass_borrows_full_text_for_confident_twin():
    """The live State-Street shape: a ', <team>' snippet borrows the bare full-text
    twin's body and would be marked hydrated."""
    jobs = [
        _snip("s1", "Senior QA Automation Engineer, Charles River Development"),
        _full("f1", "Senior QA Automation Engineer", desc="Full JD " + "y" * 3000),
    ]
    plans = plan_hydration(jobs)
    assert len(plans) == 1
    assert plans[0]["snippet_id"] == "s1" and plans[0]["source_id"] == "f1"
    assert plans[0]["description"].startswith("Full JD")


def test_hydration_pass_refuses_discipline_and_generic():
    # discipline difference -> refuse; generic 1-token snippet -> refuse.
    assert plan_hydration([_snip("s", "Software Engineer"),
                           _full("f", "Software Engineer, Frontend")]) == []
    assert plan_hydration([_snip("s", "Engineer"),
                           _full("f", "Engineer, Platform")]) == []


def test_hydration_pass_refuses_when_ambiguous():
    """Two full candidates that both look same-role (different teams) => ambiguous =>
    refuse, because we cannot be extremely sure which posting the snippet is."""
    jobs = [
        _snip("s", "Senior Backend Engineer"),
        _full("f1", "Senior Backend Engineer, Payments"),
        _full("f2", "Senior Backend Engineer, Billing"),
    ]
    assert plan_hydration(jobs) == []


def test_hydration_pass_requires_company_and_location_anchor():
    # different location -> no anchor match; missing company -> refuse outright.
    assert plan_hydration([_snip("s", "Backend Engineer", location="Cork"),
                           _full("f", "Backend Engineer, Payments", location="Dublin")]) == []
    assert plan_hydration([_snip("s", "Backend Engineer", company=""),
                           _full("f", "Backend Engineer, Payments")]) == []


def test_hydration_pass_refuses_different_company():
    assert plan_hydration([_snip("s", "Backend Engineer", company="Stripe"),
                           _full("f", "Backend Engineer, Payments", company="Square")]) == []


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
    # `hydrated` sits between snippet and full: a re-scraped snippet cannot clobber a
    # hydrated row, but genuine full text still upgrades it.
    assert completeness_rank("full") > completeness_rank("hydrated") > completeness_rank("snippet")
    assert record_precedence("api", "hydrated") > record_precedence("api", "snippet")
    assert record_precedence("api", "full") > record_precedence("api", "hydrated")


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


# --------------------------------------------------------------------------- #
# Employment-type + seniority classification (deterministic, no LLM)
# --------------------------------------------------------------------------- #
def test_classify_job_from_structured_raw_fields():
    """Structured connector fields decide first: Adzuna contract_type/contract_time,
    JSearch job_employment_type, The Muse levels."""
    assert classify_job("Backend Engineer", {"contract_type": "contract"})[0] == "contract"
    assert classify_job("Backend Engineer", {"contract_time": "part_time"})[0] == "part_time"
    assert classify_job("Backend Engineer", {"contract_type": "permanent"})[0] == "full_time"
    assert classify_job("Data Analyst", {"job_employment_type": "CONTRACTOR"})[0] == "contract"
    assert classify_job("Data Analyst", {"job_employment_type": "INTERN"})[0] == "internship"
    assert classify_job("Data Analyst", {"levels": ["Senior Level"]})[1] == "senior"
    assert classify_job("Data Analyst", {"levels": ["Entry Level"]})[1] == "junior"


def test_classify_job_from_title_heuristic():
    """When the source carries no structured field, the title-keyword fallback classifies
    (covers the crawler + ATS boards)."""
    assert classify_job("Senior Software Engineer", None) == (None, "senior")
    assert classify_job("Graduate Software Engineer", None)[1] == "junior"  # freshers
    assert classify_job("Software Engineer Intern", None) == ("internship", "intern")
    assert classify_job("QA Engineer (Contract)", None)[0] == "contract"
    assert classify_job("Engineering Manager", None)[1] == "manager"
    assert classify_job("Part-Time Support Agent", None)[0] == "part_time"


def test_classify_job_word_boundary_guards():
    """The matcher is word-boundary-ish: 'internal' is not an intern, and a plain
    'Software Engineer' with no signal stays unclassified (never guessed)."""
    assert classify_job("Internal Audit Associate", None)[1] != "intern"
    assert classify_job("Software Engineer", None) == (None, None)
    # A structured field always wins over a silent title.
    assert classify_job("Software Engineer", {"job_employment_type": "FULLTIME"})[0] == "full_time"


def test_normalize_raw_sets_employment_and_seniority():
    job = normalize_raw({
        "connector": "adzuna", "source": "adzuna", "source_type": "api",
        "title": "Senior Backend Engineer", "company": "Stripe", "location": "Dublin",
        "url": "https://adzuna/x", "raw": {"contract_type": "contract"},
    })
    assert job.employment_type == "contract"
    assert job.seniority == "senior"
    assert job.normalized_json["employment_type"] == "contract"


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


async def test_hydrate_snippet_jobs_end_to_end(mcp_services):
    """Level 2 via db-mcp: a snippet whose title carries a team suffix (so it did NOT
    collapse in Level 1) borrows the full-text twin's body and is marked 'hydrated',
    while a same-employer role with a discipline difference is left untouched."""
    db = mcp_services["db"]

    def job(connector, source_type, title, completeness, desc, url):
        return normalize_raw({"connector": connector, "source": connector,
                              "source_type": source_type, "company": "State Street",
                              "title": title, "location": "Dublin", "description": desc,
                              "description_completeness": completeness, "url": url}).model_dump()

    uid = await _mk_user()
    try:
        await _call(db, uid, "upsert_jobs", {"jobs": [
            # confident twin: team-suffixed snippet + bare full-text ATS row
            job("careerjet", "api", "Senior QA Automation Engineer, Charles River Development",
                "snippet", "teaser", "https://careerjet/a"),
            job("greenhouse:statestreet", "ats", "Senior QA Automation Engineer",
                "full", "FULL BODY " + "z" * 2000, "https://boards.greenhouse.io/ss/b"),
            # discipline difference at same employer -> must stay snippet
            job("careerjet", "api", "Software Engineer", "snippet", "teaser", "https://careerjet/c"),
            job("greenhouse:statestreet", "ats", "Software Engineer, Frontend",
                "full", "FE BODY " + "q" * 2000, "https://boards.greenhouse.io/ss/d"),
        ]})
        res = await _call(db, uid, "hydrate_snippet_jobs", {})
        assert res["hydrated"] == 1, res

        rows = {r["title"]: r for r in (await _call(db, uid, "get_jobs", {"limit": 100}))["jobs"]}
        twin = rows["Senior QA Automation Engineer, Charles River Development"]
        assert twin["description_completeness"] == "hydrated"
        assert twin["description"].startswith("FULL BODY")
        # the discipline-different snippet was NOT hydrated
        se = rows["Software Engineer"]
        assert se["description_completeness"] == "snippet" and se["description"] == "teaser"
        # idempotent: a second pass hydrates nothing new
        assert (await _call(db, uid, "hydrate_snippet_jobs", {}))["hydrated"] == 0
    finally:
        await _cleanup(uid)


async def test_get_jobs_filters_by_employment_type_and_seniority(mcp_services):
    """get_jobs narrows by employment_type / seniority (the 'contract only' / 'freshers'
    filters). A NULL-classified job never matches a filter."""
    db = mcp_services["db"]

    def job(title, company, url, raw=None):
        return normalize_raw({"connector": "adzuna", "source": "adzuna", "source_type": "api",
                              "title": title, "company": company, "location": "Dublin",
                              "url": url, "raw": raw or {}}).model_dump()

    uid = await _mk_user()
    try:
        await _call(db, uid, "upsert_jobs", {"jobs": [
            job("QA Engineer", "A", "https://a/1", {"contract_type": "contract"}),   # contract
            job("Graduate Developer", "B", "https://b/2"),                          # junior (freshers)
            job("Senior Platform Engineer", "C", "https://c/3"),                    # senior
            job("Software Engineer", "D", "https://d/4"),                           # unclassified (NULL)
        ]})
        contract = (await _call(db, uid, "get_jobs", {"employment_type": "contract"}))["jobs"]
        assert [r["title"] for r in contract] == ["QA Engineer"]
        juniors = (await _call(db, uid, "get_jobs", {"seniority": "junior"}))["jobs"]
        assert [r["title"] for r in juniors] == ["Graduate Developer"]
        # No filter -> all four (including the unclassified one).
        assert len((await _call(db, uid, "get_jobs", {"limit": 100}))["jobs"]) == 4
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
    import re as _re

    from sources_mcp.connectors.crawl import _extract_posting_links

    pattern = _re.compile(r"^/job/[^/]+/[^/]+")
    base = "https://www.irishjobs.ie/jobs/software-developer/in-dublin"
    page = """
      <a href="/job/software-engineer/acme-job101">A</a>
      <a href="/job/software-engineer/acme-job101?src=x">dup with query</a>
      <a href="https://www.irishjobs.ie/job/data-eng/globex-job202">B</a>
      <a href="/job/software-engineer/acme-job101/apply">apply (excluded)</a>
      <a href="/jobs/software-developer/in-cork">search (excluded)</a>
      <a href="/careeradvice/cv-tips">advice (excluded)</a>
    """
    links = _extract_posting_links(page, base, pattern)
    assert links == [
        "https://www.irishjobs.ie/job/software-engineer/acme-job101",
        "https://www.irishjobs.ie/job/data-eng/globex-job202",
    ]


def test_crawler_collects_jobsireland_rows():
    """Rows come back with their title attached, and the ?id= query must survive — the
    generic link extractor strips queries, which would collapse every row onto one URL."""
    from sources_mcp.connectors.crawl import _collect_jobsireland_rows

    base = "https://jobsireland.ie/Jobsireland.API/JobsIreland/BrowseJobs?Keyword=analyst"
    page = """
      <li><h3 class="title-box">Financial Analyst</h3>
        <a aria-label="View details of Financial Analyst" href="/en-US/job-Details?id=2470933">go</a></li>
      <li><h3 class="title-box">Supply Chain Analyst</h3>
        <a aria-label="View details of Supply Chain Analyst" href="/en-US/job-Details?id=2470199">go</a></li>
      <li><a href="/en-US/job-Details?id=2470933">duplicate row link</a></li>
      <li><a href="/en-US/browse-jobs">search (excluded)</a></li>
    """
    rows = _collect_jobsireland_rows(page, base)
    assert [r["url"] for r in rows] == [
        "https://jobsireland.ie/en-US/job-Details?id=2470933",
        "https://jobsireland.ie/en-US/job-Details?id=2470199",
    ]
    assert rows[0]["title"] == "Financial Analyst"


def test_crawler_parses_jobsireland_markup():
    """No JSON-LD on this board: the employer is what sits between the title and the
    reference, the address is what sits between the reference and the position count, and
    the body is span.margin-reset plus the structured requirements block."""
    from sources_mcp.connectors.crawl import _parse_jobsireland

    page = """
    <html><body>
      <h2>Job Details</h2>
      <h3>Business Analyst</h3>
      <span>ACME COMMERCIAL LIMITED</span>
      <span>Ref: #JOB-2461055</span>
      <span>Castlebar Rtl Pk, Castlebar, Co. Mayo, F23 N996</span>
      <span>No of positions: 6</span>
      <h3>Job Description</h3>
      <span class="margin-reset">Reporting to the Operations Manager, you will own KPI reporting.</span>
      <div class="experiences">Career Level Experienced Minimum Experience Required (Years): 3</div>
      <script>var t = 1;</script>
    </body></html>
    """
    data = _parse_jobsireland(page)
    assert data["title"] == "Business Analyst"
    assert data["company"] == "ACME COMMERCIAL LIMITED"
    assert data["location"] == "Castlebar Rtl Pk, Castlebar, Co. Mayo, F23 N996"
    assert data["reference"] == "2461055"
    assert "KPI reporting" in data["description"]
    assert "Minimum Experience Required" in data["description"]  # requirements appended
    assert "var t" not in data["description"]  # script stripped, not read as text


def test_crawler_parses_jobsireland_withheld_employer():
    """"Company Details Confidential" is the site's label for a withheld employer, not a
    company called "Company Details"."""
    from sources_mcp.connectors.crawl import _parse_jobsireland

    page = """
      <h3>Systems Support Analyst</h3> Company Details Confidential
      <span>Ref: #JOB-2463136</span> Athlone, Co. Westmeath <span>No of positions: 1</span>
      <span class="margin-reset">Support the systems team.</span>
    """
    assert _parse_jobsireland(page)["company"] == "Confidential"


def test_crawler_searches_the_whole_role_then_its_words():
    """jobsireland matches the keyword as an ordered phrase inside the title, so the whole
    role is asked first (it is exactly right when the role is a real title, and survives the
    page cap that a broad single word can hit), then its words, longest first."""
    from sources_mcp.connectors.crawl import _search_terms

    assert _search_terms(["Finance Data Analyst"], []) == [
        "Finance Data Analyst", "finance", "analyst", "data",
    ]
    # A role whose only real word is the noun is not worth asking for as a phrase: the
    # grade word can only narrow it, and "Junior Analyst" measured 0 against "analyst"'s 25.
    assert _search_terms(["Junior Analyst"], []) == ["analyst"]
    # A single-word role is already its own phrase; don't ask for it twice.
    assert _search_terms([], ["Python"]) == ["python"]
    # At most four requests per run.
    assert len(_search_terms(["Senior Data Platform Reliability Engineer"], [])) == 4


def test_crawler_slug_text_reads_a_title_from_the_url():
    """The pre-filter matches on the posting slug, so ids and separators must fall away."""
    from sources_mcp.connectors.crawl import _slug_text

    assert _slug_text("https://x.ie/company/cpl/job/learner-coordinator-33390") == (
        "company cpl job learner coordinator"
    )


async def test_crawler_reports_a_refused_board_as_an_error(monkeypatch):
    """A board that 403s its search page must raise, not return an empty success.

    This is the irishjobs.ie failure exactly: returning [] made a dead source look like a
    quiet one, and the block went unnoticed while scout reported '0 jobs, no errors'.
    """
    from jobpilot_core.settings import get_settings
    from sources_mcp.connectors import crawl as crawl_mod

    class _Refused:
        status_code = 403

    async def _ok_robots(*args, **kwargs):
        return True

    async def _refuse(*args, **kwargs):
        return _Refused()

    monkeypatch.setattr(crawl_mod, "_robots_ok", _ok_robots)
    monkeypatch.setattr(crawl_mod, "_get_with_retry", _refuse)
    async def _no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr(crawl_mod, "_rate_limit", _no_wait)

    with pytest.raises(crawl_mod.CrawlUnavailable) as exc:
        await crawl_mod.run_crawl(get_settings(), board="irishjobs", roles=["data analyst"])
    assert "403" in str(exc.value)


async def test_crawler_rejects_an_unknown_board():
    from jobpilot_core.settings import get_settings
    from sources_mcp.connectors import crawl as crawl_mod

    with pytest.raises(crawl_mod.CrawlUnavailable):
        await crawl_mod.run_crawl(get_settings(), board="not-a-board")


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
# Pure unit — The Muse mapping + client-side city/term filter (no network)
# --------------------------------------------------------------------------- #
def test_muse_to_raw_full_text():
    from sources_mcp.connectors.themuse import _to_raw

    job = {
        "id": 42,
        "name": "Senior Software Engineer",
        "company": {"name": "Acme"},
        "locations": [{"name": "Dublin, Ireland"}],
        "contents": "<p>Build <b>APIs</b> in Python.</p>",
        "refs": {"landing_page": "https://www.themuse.com/jobs/acme/senior-swe"},
        "categories": [{"name": "Software Engineering"}],
    }
    raw = _to_raw(job)
    assert raw.title == "Senior Software Engineer" and raw.company == "Acme"
    assert raw.location == "Dublin, Ireland"
    assert raw.description == "Build APIs in Python."
    assert raw.description_completeness == "full"  # The Muse serves full bodies
    assert raw.source_type == "api" and raw.url.endswith("senior-swe")


def test_muse_city_filter_drops_remote_leak():
    from sources_mcp.connectors.themuse import _city_match

    # The Muse's loose location filter leaks Flexible/Remote into a city query.
    assert _city_match("Dublin, Ireland", "dublin") is True
    assert _city_match("Flexible / Remote", "dublin") is False
    assert _city_match("Cork, Ireland", "dublin") is False
    assert _city_match("anything", "") is True  # no city -> keep all


def test_muse_derive_categories_from_profile():
    from sources_mcp.connectors.themuse import _derive_categories

    cats = _derive_categories(["Senior Software Engineer"], ["Python", "Data", "AWS"])
    assert "Software Engineering" in cats and "Data and Analytics" in cats
    # Unmapped role -> no category (falls back to a broad city query).
    assert _derive_categories(["Barista"], []) == []


def test_muse_normalize_location_completes_country():
    from sources_mcp.connectors.themuse import _normalize_location

    # A bare city gets the RoI country so it matches The Muse's "City, Country" facet.
    assert _normalize_location("Dublin") == "Dublin, Ireland"
    assert _normalize_location("Cork") == "Cork, Ireland"
    # An already-qualified location is left as-is.
    assert _normalize_location("Dublin, Ireland") == "Dublin, Ireland"
    assert _normalize_location("") == ""


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
# `extract_greenhouse_slugs` was deleted along with the aggregator connectors that fed
# it. It harvested board slugs from the apply links of fetched jobs, and measured against
# the real pool it produced nothing: 0 of 398 aggregator apply URLs contained any ATS
# domain. Boards are now curated and verified in ats_boards, never guessed from a URL.


def test_merge_boards_unions_and_caps():
    from jobpilot_core.ats_boards import DEFAULT_GREENHOUSE_BOARDS, merge_boards

    boards = merge_boards(["MyCo", "tines"], cap=60)
    assert boards[0] == "myco"  # configured first, lowercased
    assert "tines" in boards
    assert len(boards) == len(set(boards))  # de-duped
    # Curated defaults are folded in.
    assert set(DEFAULT_GREENHOUSE_BOARDS).issubset(set(boards))
    # Cap is honored.
    assert len(merge_boards([f"c{i}" for i in range(100)], cap=10)) == 10


def test_default_boards_are_lowercase_and_unique():
    """The board list is mapped straight into an API path, so a stray capital or a
    duplicate is a wasted request (or a 404 recorded as a connector error)."""
    from jobpilot_core.ats_boards import DEFAULT_GREENHOUSE_BOARDS as boards

    assert boards == [b.lower().strip() for b in boards]
    assert len(boards) == len(set(boards))
    # Boards verified as having no Irish roles must stay out of an Ireland-only pool.
    for excluded in ("accenturefederalservices", "dmgmedia", "nuitee", "scalapaysrl"):
        assert excluded not in boards


# --------------------------------------------------------------------------- #
# Addendum 2.2 — description completeness tagging (no network/LLM)
# --------------------------------------------------------------------------- #
def test_connector_tags_description_completeness():
    """Every remaining connector reports `full`, and normalize_raw carries the flag
    through to the persisted shape. The snippet-emitting connectors (Adzuna, Jooble,
    Careerjet) were removed — a teaser cannot be qualified against or tailored to."""
    from sources_mcp.connectors.apify import _to_raw as apify_raw
    from sources_mcp.connectors.jsearch import _to_raw as jsearch_raw
    from sources_mcp.connectors.themuse import _to_raw as muse_raw
    from sources_mcp.connectors.workable import _board_to_raw, _feed_to_raw

    assert jsearch_raw({"job_title": "x"}).description_completeness == "full"
    assert muse_raw({"name": "x", "contents": "<p>body</p>"}).description_completeness == "full"
    assert _feed_to_raw({"title": "x", "description": "<p>b</p>"}, "Dublin").description_completeness == "full"
    assert _board_to_raw({"title": "x", "description": "<p>b</p>"}, "acme", "Acme").description_completeness == "full"
    assert apify_raw({"title": "x", "descriptionText": "body"}).description_completeness == "full"
    # Apify is the one source that can legitimately come back empty-bodied.
    assert apify_raw({"title": "x", "descriptionText": ""}).description_completeness == "snippet"

    ji = normalize_raw(_feed_to_raw({"title": "x", "company": {"title": "c"},
                                     "description": "<p>body</p>"}, "Dublin"))
    assert ji.description_completeness == "full"


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
                          {"roles": ["Backend Engineer"], "sources": ["workable", "greenhouse"],
                           "target_employers": ["tines"]})
        assert upd["config"]["sources"] == ["workable", "greenhouse"]
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
async def test_scout_workable_and_greenhouse_persist_idempotent(sources_stack):
    """Both keyless full-text sources, end to end: Workable's feed (api tier) and a
    Greenhouse board (ats tier), persisted and idempotent on re-run."""
    from app import mcp_client
    from app.scout_runner import run_scout

    uid = await _mk_user()
    try:
        await mcp_client.config_mcp(uid, "update_search_config", {
            "roles": [], "locations": ["Dublin"], "keywords": [],
            "sources": ["workable", "greenhouse"], "target_employers": ["tines"]})
        r1 = await run_scout(uid)
        assert r1["errors"] == {}
        assert r1["by_connector"].get("workable", 0) > 0
        assert r1["by_connector"].get("greenhouse:tines", 0) > 0
        assert r1["new"] == r1["unique"] > 0

        jobs = (await mcp_client.db_mcp(uid, "get_jobs", {"limit": 5000}))["jobs"]
        types = {j["source_type"] for j in jobs}
        assert {"api", "ats"} <= types
        assert len({j["dedupe_hash"] for j in jobs}) == len(jobs)  # no dup rows
        # Ireland-only, and full text throughout: the point of this source set.
        assert all(j["description_completeness"] in ("full", "hydrated") for j in jobs)

        # Idempotency, stated as the invariant that actually holds against a *live* feed:
        # re-running must not duplicate anything it already stored. Asserting new == 0
        # would be flaky — Workable's feed is a live cursor over ~900 Irish jobs, so a
        # genuinely new posting can arrive between two runs seconds apart.
        hashes_before = {j["dedupe_hash"] for j in jobs}
        r2 = await run_scout(uid)
        after = (await mcp_client.db_mcp(uid, "get_jobs", {"limit": 5000}))["jobs"]
        hashes_after = {j["dedupe_hash"] for j in after}
        assert len(hashes_after) == len(after), "re-running produced duplicate rows"
        assert hashes_before <= hashes_after, "a previously stored job disappeared"
        assert r2["new"] == len(hashes_after) - len(hashes_before)
    finally:
        await _cleanup(uid)


async def test_scout_pool_is_republic_of_ireland_only(sources_stack):
    """Every persisted job sits in the Republic. Northern Ireland is the case that
    matters: it reads as Irish and is a different jurisdiction."""
    from jobpilot_core.roi_location import is_roi

    from app import mcp_client
    from app.scout_runner import run_scout

    uid = await _mk_user()
    try:
        await mcp_client.config_mcp(uid, "update_search_config", {
            "roles": [], "locations": [], "keywords": [],
            # Boards that are mostly non-Irish; the connector filter must hold.
            "sources": ["workable", "greenhouse"],
            "target_employers": ["intercom", "stripe", "datadog"]})
        await run_scout(uid)
        jobs = (await mcp_client.db_mcp(uid, "get_jobs", {"limit": 5000}))["jobs"]
        assert jobs, "expected some jobs to verify"
        leaked = [j["location"] for j in jobs if not is_roi(j["location"])]
        assert leaked == [], f"non-RoI jobs reached the pool: {leaked[:10]}"
    finally:
        await _cleanup(uid)


async def test_scout_graceful_degradation_and_run_tracking(sources_stack):
    from app import mcp_client
    from app.scout_runner import run_scout_tracked

    uid = await _mk_user()
    try:
        await mcp_client.config_mcp(uid, "update_search_config", {
            "roles": [], "locations": ["Dublin"], "keywords": [],
            "sources": ["workable", "greenhouse"], "target_employers": ["no-such-board-xyz-123"]})
        summary = await run_scout_tracked(uid)
        assert "greenhouse:no-such-board-xyz-123" in summary["errors"]  # recorded
        assert summary["by_connector"].get("workable", 0) > 0  # good connector still ran

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
            "roles": [], "locations": ["Dublin"], "keywords": [],
            "sources": ["workable"]}))
        result = scout_run.apply(args=[str(uid)])
        summary = result.get()
        assert summary["new"] > 0 and "run_id" in summary

        runs = _run(mcp_client.db_mcp(uid, "get_pipeline_runs", {"stage": "scout"}))["runs"]
        assert len(runs) == 1 and runs[0]["status"] == "completed"
    finally:
        _run(_cleanup(uid))
