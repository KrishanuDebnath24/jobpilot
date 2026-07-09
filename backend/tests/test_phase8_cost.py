"""Phase 8b — per-user LLM cost aggregation (pure, offline).

Feeds ``pipeline_run_to_dict``-shaped rows into ``cost.aggregate_cost`` and proves the
cost report the dashboard needs: total $, breakdown by agent + by day, token totals,
the batch/cache-discount confirmation, and the daily-threshold alert.
"""

from __future__ import annotations

import json
import uuid

import pytest
from jobpilot_core import cost

from .conftest import _db_reachable

requires_db = pytest.mark.skipif(not _db_reachable(), reason="Postgres not reachable")


def _run(stage, day, usage):
    return {"stage": stage, "finished_at": f"{day}T12:00:00+00:00", "counts": {"llm_usage": usage}}


# A realistic mix: an analyze batch (naive > cost → savings), an enhance sync call, a
# scout with no crawl usage, and the umbrella row (both must contribute nothing).
_RUNS = [
    _run("analyze", "2026-07-08", {
        "model": "claude-haiku-4-5", "input_tokens": 1000, "output_tokens": 200,
        "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 500,
        "batch": True, "estimated_cost_usd": 1.0, "naive_cost_usd": 4.0}),
    _run("enhance", "2026-07-08", {
        "model": "claude-sonnet-5", "input_tokens": 2000, "output_tokens": 800,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "batch": False, "estimated_cost_usd": 3.0, "naive_cost_usd": 3.0}),
    _run("scout", "2026-07-09", {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}),
    {"stage": "pipeline", "finished_at": "2026-07-08T12:00:00+00:00",
     "counts": {"llm_usage": {"estimated_cost_usd": 999.0}}},  # umbrella — must be skipped
    {"stage": "qualify", "finished_at": "2026-07-09T00:00:00+00:00", "counts": {"gated_out": 5}},  # no usage
]


def test_totals_by_agent_and_savings():
    r = cost.aggregate_cost(_RUNS, daily_threshold_usd=100.0)
    assert r["total_cost_usd"] == 4.0            # 1.0 + 3.0 (+0 scout); umbrella 999 skipped
    assert r["total_naive_cost_usd"] == 7.0      # 4.0 + 3.0
    assert r["total_savings_usd"] == 3.0 and r["savings_pct"] > 40
    assert set(r["by_agent"]) == {"analyze", "enhance", "scout"}  # not "pipeline"/"qualify"
    assert r["by_agent"]["analyze"]["cost_usd"] == 1.0 and r["by_agent"]["analyze"]["runs"] == 1
    assert r["tokens"]["cache_read_tokens"] == 5000 and r["tokens"]["output_tokens"] == 1000


def test_batch_and_cache_discounts_are_confirmed_from_data():
    r = cost.aggregate_cost(_RUNS)
    assert r["batch_discount_active"] is True   # the analyze run was a batch
    assert r["cache_discount_active"] is True   # it had cache-read tokens


def test_by_day_and_daily_threshold_alert():
    r = cost.aggregate_cost(_RUNS, daily_threshold_usd=2.0)
    assert r["by_day"]["2026-07-08"] == 4.0 and r["by_day"]["2026-07-09"] == 0.0
    assert r["over_threshold"] is True
    assert [a["date"] for a in r["alerts"]] == ["2026-07-08"]  # only the $4 day breaches $2


def test_empty_and_usageless_runs_are_zero():
    assert cost.aggregate_cost([])["total_cost_usd"] == 0.0
    assert cost.aggregate_cost([{"stage": "scout", "counts": {}}])["over_threshold"] is False


@requires_db
async def test_cost_report_from_real_pipeline_runs():
    """DB round-trip: a run's llm_usage persisted to output_ref → get_pipeline_runs →
    aggregate. Proves the endpoint's data path (minus the thin HTTP/MCP wrapper)."""
    from jobpilot_core import repositories as R
    from jobpilot_core.db import session_scope
    from jobpilot_core.security import hash_password

    async with session_scope() as s:
        uid = (await R.create_user(s, f"p8c_{uuid.uuid4().hex}@ex.com", hash_password("x"))).id
        run = await R.create_pipeline_run(s, uid, "analyze")
        await R.finish_pipeline_run(s, uid, run.id, "success", output_ref=json.dumps({
            "analyzed": 3, "llm_usage": {
                "model": "claude-haiku-4-5", "input_tokens": 100, "output_tokens": 20,
                "cache_read_input_tokens": 400, "cache_creation_input_tokens": 0, "batch": True,
                "estimated_cost_usd": 0.5, "naive_cost_usd": 1.5}}))
    try:
        async with session_scope() as s:
            runs = await R.get_pipeline_runs(s, uid, limit=50)
        rep = cost.aggregate_cost(runs, daily_threshold_usd=100.0)
        assert rep["total_cost_usd"] == 0.5 and rep["total_savings_usd"] == 1.0
        assert rep["by_agent"]["analyze"]["runs"] == 1
        assert rep["batch_discount_active"] and rep["cache_discount_active"]
    finally:
        async with session_scope() as s:
            await R.delete_user_cascade(s, uid)
