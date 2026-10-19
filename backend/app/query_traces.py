"""Rollups over Query-agent turn traces.

Pure functions over the payloads ``query_runner`` writes to ``agent_events`` — no I/O, so
the arithmetic behind the dashboard is unit-testable offline. The summary is computed here
rather than in the browser so the numbers on screen and the numbers in a test are the same
code, and so a client that renders one trace can't quietly disagree about the fleet.
"""

from __future__ import annotations

from collections import defaultdict

# A turn that called no tools answered from the conversation alone. That is legitimate for
# a follow-up ("and how many of those are remote?" after the counts are already in
# context), and it is exactly what an ungrounded answer looks like — so it is counted and
# surfaced, never silently folded into the pass rate.
UNGROUNDED = "no_tools"


def _pct(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile. Small n by design: this reads a few hundred turns, and an
    interpolating percentile over 12 samples would invent precision the data lacks."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p / 100 * len(ordered) + 0.5) - 1))
    return round(ordered[idx], 1)


def _num(value, default=0):
    return value if isinstance(value, (int, float)) else default


def summarize_traces(traces: list[dict]) -> dict:
    """Fleet view over ``traces`` (each the ``payload`` of one ``query_turn`` event)."""
    total = len(traces)
    durations: list[float] = []
    ttfts: list[float] = []
    per_tool: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "ms": [], "rows": []})
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    cost = naive_cost = 0.0
    errors = ungrounded = uncited = routed = tool_error_turns = 0

    for t in traces:
        if t.get("status") == "error":
            errors += 1
        if _num(t.get("duration_ms")):
            durations.append(float(t["duration_ms"]))
        if _num(t.get("ttft_ms")):
            ttfts.append(float(t["ttft_ms"]))
        if _num(t.get("tool_errors")):
            tool_error_turns += 1
        if t.get("routed_to_concierge"):
            routed += 1

        spans = t.get("spans") or []
        tool_spans = [s for s in spans if s.get("kind") == "tool"]
        if not tool_spans:
            ungrounded += 1
        elif not _num(t.get("citations")) and any(
            isinstance(s.get("rows"), int) and s["rows"] > 0 for s in tool_spans
        ):
            # It pulled ROWS back and cited none of them — the answer may still be right,
            # but nothing on screen tells the user which ones it rests on. Gated on rows
            # rather than on tool names: a count returns a number with nothing to link,
            # so a counting turn citing nothing is correct and must not be flagged.
            uncited += 1
        for span in tool_spans:
            entry = per_tool[span.get("name") or "?"]
            entry["calls"] += 1
            entry["errors"] += span.get("status") == "error"
            if _num(span.get("ms")):
                entry["ms"].append(float(span["ms"]))
            if isinstance(span.get("rows"), int):
                entry["rows"].append(span["rows"])

        usage = t.get("usage") or {}
        tokens["input"] += int(_num(usage.get("input_tokens")))
        tokens["output"] += int(_num(usage.get("output_tokens")))
        tokens["cache_read"] += int(_num(usage.get("cache_read_input_tokens")))
        tokens["cache_creation"] += int(_num(usage.get("cache_creation_input_tokens")))
        cost += float(_num(usage.get("estimated_cost_usd")))
        naive_cost += float(_num(usage.get("naive_cost_usd")))

    billed_input = tokens["input"] + tokens["cache_read"] + tokens["cache_creation"]
    tools = [
        {
            "tool": name,
            "calls": e["calls"],
            "errors": e["errors"],
            "error_pct": round(100 * e["errors"] / e["calls"], 1) if e["calls"] else 0.0,
            "p50_ms": _pct(e["ms"], 50),
            "p95_ms": _pct(e["ms"], 95),
            "avg_rows": round(sum(e["rows"]) / len(e["rows"]), 1) if e["rows"] else None,
        }
        for name, e in per_tool.items()
    ]
    tools.sort(key=lambda t: (-t["calls"], t["tool"]))

    return {
        "turns": total,
        "errors": errors,
        "tool_error_turns": tool_error_turns,
        "ungrounded_turns": ungrounded,
        "uncited_turns": uncited,
        "routed_to_concierge": routed,
        "latency_ms": {
            "p50": _pct(durations, 50), "p95": _pct(durations, 95),
            "max": round(max(durations), 1) if durations else None,
        },
        "ttft_ms": {"p50": _pct(ttfts, 50), "p95": _pct(ttfts, 95)},
        "tokens": tokens,
        # Share of billed input tokens served from the cached prefix — the system prompt
        # and tool defs are deliberately byte-stable to make this number high, so a drop
        # here means someone broke that.
        "cache_hit_pct": round(100 * tokens["cache_read"] / billed_input, 1) if billed_input else 0.0,
        "cost_usd": round(cost, 6),
        "cost_per_turn_usd": round(cost / total, 6) if total else 0.0,
        "naive_cost_usd": round(naive_cost, 6),
        "savings_pct": round(100 * (1 - cost / naive_cost), 1) if naive_cost > 0 else 0.0,
        "tools": tools,
    }
