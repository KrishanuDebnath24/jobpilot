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
from jobpilot_core.scout import canonicalize_url, compute_dedupe_hash, normalize_raw
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
