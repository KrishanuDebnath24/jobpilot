"""Phase 5 tests — Analyzer + Enhancer, batch/cache accounting, aggregation.

Pure-unit tests (aggregation determinism, cost/savings math, prompt cache shaping,
schema shape) run anywhere. The persistence round-trip uses db-mcp against Postgres
and skips cleanly when the DB is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from jobpilot_core import analyzer, enhancer, repositories
from jobpilot_core.db import session_scope
from jobpilot_core.llm import LLMUsage, build_forced_tool_params
from jobpilot_core.scout import normalize_raw
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
        u = await repositories.create_user(s, f"p5_{uuid.uuid4().hex}@ex.com", hash_password("x"))
        return u.id


async def _cleanup(uid: uuid.UUID) -> None:
    async with session_scope() as s:
        await repositories.delete_user_cascade(s, uid)


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
# 5c — deterministic aggregation (counting, no LLM)
# --------------------------------------------------------------------------- #
def test_aggregate_counts_frequencies_deterministically():
    analyses = [
        {"required_skills": ["Python", "Kubernetes"], "keywords": ["api"],
         "seniority": "senior", "tech_stack": ["AWS"], "gaps_vs_resume": ["no k8s"]},
        {"required_skills": ["python", "Go"], "keywords": ["API"],
         "seniority": "Senior", "tech_stack": ["aws"], "gaps_vs_resume": ["no k8s"]},
        {"required_skills": ["Python"], "keywords": [],
         "seniority": "mid", "tech_stack": [], "gaps_vs_resume": []},
    ]
    agg = analyzer.aggregate(analyses)
    assert agg["total_jobs"] == 3
    freq = {r["label"]: r["pct"] for r in agg["skill_frequency"]}
    # normalized: Python in all 3 -> 100%; Kubernetes/Go each in 1 -> 33.3%
    assert freq["Python"] == 100.0
    assert freq["Kubernetes"] == 33.3
    # recurring gap present in 2/3 jobs -> 66.7%
    gaps = {r["label"]: r["pct"] for r in agg["recurring_gaps"]}
    assert gaps["no k8s"] == 66.7
    # seniority normalized (Senior==senior) -> 2 senior, 1 mid
    sd = {r["label"]: r["count"] for r in agg["seniority_distribution"]}
    assert sd["senior"] == 2 and sd["mid"] == 1
    # deterministic: same input -> same output
    assert analyzer.aggregate(analyses) == agg


def test_aggregate_excludes_snippets_and_states_basis():
    """Addendum 2.2 (revised) step 2: frequency stats compute over FULL-TEXT jobs only.
    Excluding snippet-only analyses (limited_data) makes the numbers RISE (correcting the
    snippet-diluted undercount), and the aggregate states its basis 'N full-text of M'."""
    analyses = [
        {"required_skills": ["Python"], "seniority": "senior", "limited_data": False},
        {"required_skills": ["Python"], "seniority": "senior", "limited_data": False},
        # two snippet-only jobs that DON'T mention Python — pure dilution
        {"required_skills": [], "seniority": "unknown", "limited_data": True},
        {"required_skills": [], "seniority": "unknown", "limited_data": True},
    ]
    agg = analyzer.aggregate(analyses)
    assert agg["total_jobs"] == 4 and agg["full_text_jobs"] == 2 and agg["snippet_jobs"] == 2
    assert agg["basis"] == "computed from 2 full-text jobs of 4 qualified"
    # Python is in 2/2 full-text jobs -> 100% (would be a diluted 50% over all 4).
    freq = {r["label"]: r["pct"] for r in agg["skill_frequency"]}
    assert freq["Python"] == 100.0
    # seniority spread is full-text only: the two snippet 'unknown' rows are excluded.
    sd = {r["label"]: r["count"] for r in agg["seniority_distribution"]}
    assert sd == {"senior": 2}


# --------------------------------------------------------------------------- #
# 5b — batch + cache cost accounting proves the savings
# --------------------------------------------------------------------------- #
def test_usage_cost_reflects_cache_and_batch_savings():
    # 10 jobs, each 1000 cached-prefix reads + 200 fresh input + 150 output, batched.
    u = LLMUsage(model="claude-haiku-4-5-20251001", batch=True)
    for _ in range(10):
        u.add(200, 150, cache_read=1000, cache_creation=0)
    d = u.as_dict()
    assert d["batch"] is True
    assert d["cache_read_input_tokens"] == 10000
    # cache read is ~0.1x and batch halves everything -> real cost well below naive
    assert d["estimated_cost_usd"] < d["naive_cost_usd"]
    assert d["savings_pct"] > 40  # batch(50%) + cache(90% on the cached portion)


def test_cached_prefix_places_cache_control_on_the_stable_block():
    params = build_forced_tool_params(
        system="sys", cached_prefix="STABLE PREFIX", variable_text="VARIABLE",
        tool_name="t", tool_description="d", input_schema={"type": "object"},
        model="m", max_tokens=100,
    )
    content = params["messages"][0]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["text"] == "STABLE PREFIX"
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in content[1]  # variable suffix is not cached


# --------------------------------------------------------------------------- #
# 5a — the Analyzer prompt prefix is STABLE across jobs (so it caches)
# --------------------------------------------------------------------------- #
def test_analyzer_prefix_is_stable_across_jobs():
    resume = {"skills": ["Python"], "summary": "eng", "experience": [], "education": []}
    stable_a, var_a = analyzer.build_prompt(resume, {"title": "A", "description": "x"}, S)
    stable_b, var_b = analyzer.build_prompt(resume, {"title": "B", "description": "y"}, S)
    assert stable_a == stable_b  # identical prefix -> cache hit after first write
    assert var_a != var_b  # variable suffix differs per job


# --------------------------------------------------------------------------- #
# 5d — Enhancer schema enforces the three honest buckets + no-fabrication guard
# --------------------------------------------------------------------------- #
def test_enhancer_schema_has_three_honest_buckets():
    props = enhancer._SCHEMA["properties"]
    assert set(props) == {"skills_to_add", "projects", "bullet_rewrites"}
    # skills carry grounding + a truthful-basis field (never fabricate)
    skill_props = props["skills_to_add"]["items"]["properties"]
    assert "why" in skill_props and "truthful_basis" in skill_props
    assert "fabricat" in enhancer._INTEGRITY.lower()  # explicit no-fabrication rule


# --------------------------------------------------------------------------- #
# Persistence round-trip (db-mcp; skips without a DB)
# --------------------------------------------------------------------------- #
async def test_analysis_and_suggestion_roundtrip(mcp_services):
    db = mcp_services["db"]
    uid = await _mk_user()
    try:
        job = normalize_raw({
            "connector": "greenhouse:acme", "source": "greenhouse:acme", "source_type": "ats",
            "company": "Acme", "title": "Backend Engineer", "location": "Dublin",
            "url": "https://boards.greenhouse.io/acme/1", "description": "Python, Kubernetes",
            "description_completeness": "full",
        }).model_dump()
        await _call(db, uid, "upsert_jobs", {"jobs": [job]})
        job_id = (await _call(db, uid, "get_jobs", {"limit": 10}))["jobs"][0]["id"]

        # analysis
        await _call(db, uid, "upsert_analysis", {
            "job_id": job_id,
            "extracted_json": {"required_skills": ["Python", "Kubernetes"], "limited_data": False},
            "gaps_json": {"gaps": ["no k8s"]}, "seniority": "senior"})
        analyses = (await _call(db, uid, "get_analyses", {}))["analyses"]
        assert len(analyses) == 1 and analyses[0]["seniority"] == "senior"
        assert analyses[0]["extracted_json"]["required_skills"] == ["Python", "Kubernetes"]
        # re-analyze overwrites (idempotent)
        await _call(db, uid, "upsert_analysis", {
            "job_id": job_id, "extracted_json": {"required_skills": ["Go"]}, "seniority": "mid"})
        analyses = (await _call(db, uid, "get_analyses", {}))["analyses"]
        assert len(analyses) == 1 and analyses[0]["seniority"] == "mid"

        # suggestion (job-scoped) + lifecycle
        await _call(db, uid, "upsert_suggestion", {
            "job_id": job_id, "skills_to_add": ["Kubernetes"],
            "projects_json": {"projects": [{"title": "K8s lab", "builds_skill": "Kubernetes", "description": "..."}]},
            "bullet_rewrites_json": {"bullet_rewrites": []}})
        sugg = (await _call(db, uid, "get_suggestions", {}))["suggestions"]
        assert len(sugg) == 1 and sugg[0]["status"] == "proposed"
        assert sugg[0]["skills_to_add"] == ["Kubernetes"]
        sid = sugg[0]["id"]
        await _call(db, uid, "update_suggestion_status", {"suggestion_id": sid, "status": "accepted"})
        sugg = (await _call(db, uid, "get_suggestions", {}))["suggestions"]
        assert sugg[0]["status"] == "accepted"

        # aggregate suggestion (job_id NULL) coexists; re-run replaces prior aggregate
        await _call(db, uid, "upsert_suggestion", {"job_id": None, "skills_to_add": ["Docker"]})
        await _call(db, uid, "upsert_suggestion", {"job_id": None, "skills_to_add": ["Terraform"]})
        aggs = [s for s in (await _call(db, uid, "get_suggestions", {}))["suggestions"] if s["job_id"] is None]
        assert len(aggs) == 1 and aggs[0]["skills_to_add"] == ["Terraform"]
    finally:
        await _cleanup(uid)
