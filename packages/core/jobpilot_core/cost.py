"""Per-user LLM cost aggregation (Phase 8b — cost controls).

Pure and deterministic: given a user's ``pipeline_runs`` (each carrying the run summary
the runners persist to ``output_ref`` — counts plus an ``llm_usage`` block), roll them up
into a cost report: total $, a breakdown **by agent/stage** and **by day**, token totals,
a confirmation that the **batch (50%) + cache** discounts are active, and a soft
**daily-threshold alert**. No LLM and no DB here — the backend reads the runs via db-mcp
and feeds them in — so this is unit-testable offline.

Cost figures are ESTIMATES summed from the same ``LLMUsage`` math used at call time
(billing is authoritative). ``naive_cost_usd`` (no cache, no batch) is carried on the
richer summaries, so the report can quantify the savings that discount strategy buys.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# The per-execution umbrella row is a correlation/rollup marker, not an LLM stage — skip
# it so its (absent or aggregate) usage never double-counts the stage rows beneath it.
_NON_LLM_STAGES = {"pipeline"}


def _usage_of(run: dict) -> dict | None:
    counts = run.get("counts")
    if isinstance(counts, dict):
        u = counts.get("llm_usage")
        if isinstance(u, dict):
            return u
    return None


def _day(run: dict) -> str:
    ts = run.get("finished_at") or run.get("created_at") or ""
    return ts[:10] if isinstance(ts, str) else ""


def _num(u: dict, key: str) -> float:
    try:
        return float(u.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def aggregate_cost(runs: list[dict], *, daily_threshold_usd: float = 5.0) -> dict:
    """Aggregate ``pipeline_run_to_dict`` rows into a per-user cost report. Runs without
    ``llm_usage`` (Scout with no crawl, gated-out Qualify, gates, the umbrella row)
    contribute nothing."""
    total_cost = total_naive = 0.0
    tokens: dict[str, int] = defaultdict(int)
    by_agent: dict[str, dict] = {}
    by_day: dict[str, float] = defaultdict(float)
    batch_active = cache_active = False

    for run in runs:
        if run.get("stage") in _NON_LLM_STAGES:
            continue
        u = _usage_of(run)
        if not u:
            continue

        cost = _num(u, "estimated_cost_usd")
        # scout/qualify summaries lack naive_cost_usd → no discount modelled, use cost
        naive = _num(u, "naive_cost_usd") or cost
        total_cost += cost
        total_naive += naive

        cr = int(_num(u, "cache_read_input_tokens"))
        cw = int(_num(u, "cache_creation_input_tokens"))
        it = int(_num(u, "input_tokens"))
        ot = int(_num(u, "output_tokens"))
        tokens["input_tokens"] += it
        tokens["output_tokens"] += ot
        tokens["cache_read_tokens"] += cr
        tokens["cache_write_tokens"] += cw
        batch_active = batch_active or bool(u.get("batch"))
        cache_active = cache_active or cr > 0

        stage = run.get("stage") or "unknown"
        a = by_agent.setdefault(
            stage, {"cost_usd": 0.0, "runs": 0, "input_tokens": 0, "output_tokens": 0})
        a["cost_usd"] = round(a["cost_usd"] + cost, 6)
        a["runs"] += 1
        a["input_tokens"] += it
        a["output_tokens"] += ot

        by_day[_day(run)] += cost

    savings = max(0.0, total_naive - total_cost)
    alerts = [
        {"date": d, "cost_usd": round(c, 6)}
        for d, c in sorted(by_day.items())
        if c > daily_threshold_usd
    ]
    return {
        "total_cost_usd": round(total_cost, 6),
        "total_naive_cost_usd": round(total_naive, 6),
        "total_savings_usd": round(savings, 6),
        "savings_pct": round(100 * savings / total_naive, 1) if total_naive > 0 else 0.0,
        "tokens": dict(tokens),
        "by_agent": by_agent,
        "by_day": {d: round(c, 6) for d, c in sorted(by_day.items())},
        "batch_discount_active": batch_active,
        "cache_discount_active": cache_active,
        "daily_threshold_usd": daily_threshold_usd,
        "alerts": alerts,
        "over_threshold": bool(alerts),
    }
