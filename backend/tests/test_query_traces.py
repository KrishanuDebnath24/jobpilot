"""Query-agent tracing: what one turn records, and the rollup the dashboard renders.

All offline — no DB, no LLM. The turn is driven through the same fake-stream harness the
Phase 7 turn tests use, so a trace is asserted against the real loop (real spans, real
concurrency, real fold-back order) rather than a hand-built payload.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from jobpilot_core import query_agent

from app.query_traces import summarize_traces


# --------------------------------------------------------------------------- #
# Harness — prepared model responses + a fake MCP dispatch
# --------------------------------------------------------------------------- #
class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ToolUse:
    type = "tool_use"

    def __init__(self, name, args, id_="tu"):
        self.name, self.input, self.id = name, args, id_


class _Usage:
    def __init__(self, i=0, o=0, cr=0, cc=0):
        self.input_tokens, self.output_tokens = i, o
        self.cache_read_input_tokens, self.cache_creation_input_tokens = cr, cc


class _Resp:
    def __init__(self, stop_reason, content, usage=None):
        self.stop_reason, self.content = stop_reason, content
        if usage is not None:
            self.usage = usage


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for block in self._resp.content:
                if getattr(block, "type", None) == "text":
                    yield block.text

        return gen()

    async def get_final_message(self):
        return self._resp


def _patch_turn(monkeypatch, rounds, call, *, on_event=None):
    """Point the turn at prepared responses + a fake MCP dispatch, capturing every
    ``record_agent_event`` write in the returned list."""
    from app import query_runner as qr

    class _Messages:
        def stream(self, **kw):
            return _FakeStream(rounds.pop(0))

    class _Client:
        messages = _Messages()

        async def close(self):
            pass

    written: list[dict] = []

    async def fake_db_mcp(user_id, tool, arguments=None):
        if tool == "get_messages":
            return {"messages": []}
        if tool == "record_agent_event":
            if on_event is not None:
                on_event(arguments)
            written.append(arguments or {})
        return {}

    monkeypatch.setattr(qr, "get_client", lambda settings: _Client())
    monkeypatch.setattr(qr, "_default_call", call)
    monkeypatch.setattr(qr.mcp_client, "db_mcp", fake_db_mcp)
    return qr, written


async def _counts(user_id, service, tool, args):
    await asyncio.sleep(0.02)
    return {"count": 279}


# --------------------------------------------------------------------------- #
# What a turn records
# --------------------------------------------------------------------------- #
async def test_turn_writes_one_trace_with_llm_and_tool_spans(monkeypatch):
    rounds = [
        _Resp("tool_use", [_Text("Checking. "), _ToolUse("count_jobs", {}, "t1")],
              _Usage(1200, 40, cr=8000, cc=0)),
        _Resp("end_turn", [_Text("You have 279 jobs.")], _Usage(300, 60, cr=9000)),
    ]
    qr, written = _patch_turn(monkeypatch, rounds, _counts)

    out = await qr.run_query_turn(uuid.uuid4(), uuid.uuid4(), "how many jobs?")
    assert out["answer"] == "Checking. You have 279 jobs."
    assert len(written) == 1  # exactly one trace per turn, not one per round
    ev = written[0]
    assert ev["type"] == qr.TRACE_EVENT
    assert ev["keep"] > 0  # self-pruning: the log is bounded without a sweeper

    trace = ev["payload_json"]
    assert trace["status"] == "ok" and trace["rounds"] == 2
    assert trace["tools"] == ["count_jobs"] and trace["tool_errors"] == 0
    kinds = [s["kind"] for s in trace["spans"]]
    assert kinds == ["llm", "tool", "llm"]  # round -> tool -> round, in order
    # Token usage is summed across rounds, and cost priced off the cached read.
    usage = trace["usage"]
    assert usage["input_tokens"] == 1500 and usage["output_tokens"] == 100
    assert usage["cache_read_input_tokens"] == 17000
    assert usage["estimated_cost_usd"] > 0
    assert usage["naive_cost_usd"] > usage["estimated_cost_usd"]  # caching is the point


async def test_tool_span_carries_duration_args_and_result_shape(monkeypatch):
    async def slow_search(user_id, service, tool, args):
        await asyncio.sleep(0.05)
        return {"jobs": [{"id": str(uuid.uuid4()), "title": "SRE"} for _ in range(3)]}

    rounds = [
        _Resp("tool_use", [_ToolUse("semantic_search_jobs", {"query": "data platforms"}, "t1")]),
        _Resp("end_turn", [_Text("Three roles match.")]),
    ]
    qr, written = _patch_turn(monkeypatch, rounds, slow_search)
    monkeypatch.setattr(qr, "embed_texts", lambda texts, settings=None: [[0.1, 0.2]])

    await qr.run_query_turn(uuid.uuid4(), uuid.uuid4(), "roles about data platforms?")
    span = next(s for s in written[0]["payload_json"]["spans"] if s["kind"] == "tool")
    assert span["ms"] >= 40  # the call's own time, not the round's
    assert "data platforms" in span["args"]  # what it actually searched for
    assert span["rows"] == 3 and span["bytes"] > 0
    assert span["status"] == "ok" and span["error"] is None


async def test_parallel_tools_share_a_start_offset(monkeypatch):
    """The round's tools go out together, so the waterfall must show them overlapping —
    a per-tool start taken after the gather would draw them as a staircase."""
    rounds = [
        _Resp("tool_use", [_ToolUse("count_jobs", {}, "a"),
                           _ToolUse("job_stats", {}, "b"),
                           _ToolUse("count_qualifications", {}, "c")]),
        _Resp("end_turn", [_Text("done")]),
    ]
    qr, written = _patch_turn(monkeypatch, rounds, _counts)

    await qr.run_query_turn(uuid.uuid4(), uuid.uuid4(), "how many jobs?")
    tools = [s for s in written[0]["payload_json"]["spans"] if s["kind"] == "tool"]
    assert len(tools) == 3
    assert len({s["t"] for s in tools}) == 1  # one shared start offset
    # …and the turn is not the sum of their durations, because they ran at once.
    assert written[0]["payload_json"]["duration_ms"] < sum(s["ms"] for s in tools)


async def test_failed_tool_is_traced_without_failing_the_turn(monkeypatch):
    async def broken(user_id, service, tool, args):
        raise RuntimeError("db-mcp unreachable")

    rounds = [
        _Resp("tool_use", [_ToolUse("count_jobs", {}, "t1")]),
        _Resp("end_turn", [_Text("No data for that.")]),
    ]
    qr, written = _patch_turn(monkeypatch, rounds, broken)

    out = await qr.run_query_turn(uuid.uuid4(), uuid.uuid4(), "how many jobs?")
    assert out["answer"] == "No data for that."  # the turn survives a bad tool
    trace = written[0]["payload_json"]
    assert trace["status"] == "ok" and trace["tool_errors"] == 1
    span = next(s for s in trace["spans"] if s["kind"] == "tool")
    assert span["status"] == "error" and "unreachable" in span["error"]


async def test_turn_that_raises_is_still_traced(monkeypatch):
    from app import query_runner as qr

    class _Exploding:
        async def __aenter__(self):
            raise RuntimeError("anthropic 529")

        async def __aexit__(self, *exc):
            return False

    class _Messages:
        def stream(self, **kw):
            return _Exploding()

    class _Client:
        messages = _Messages()

        async def close(self):
            pass

    written: list[dict] = []

    async def fake_db_mcp(user_id, tool, arguments=None):
        if tool == "get_messages":
            return {"messages": []}
        if tool == "record_agent_event":
            written.append(arguments or {})
        return {}

    monkeypatch.setattr(qr, "get_client", lambda settings: _Client())
    monkeypatch.setattr(qr.mcp_client, "db_mcp", fake_db_mcp)

    with pytest.raises(RuntimeError):
        await qr.run_query_turn(uuid.uuid4(), uuid.uuid4(), "how many jobs?")
    assert len(written) == 1
    trace = written[0]["payload_json"]
    assert trace["status"] == "error" and "529" in trace["error"]


async def test_trace_write_failure_never_breaks_the_turn(monkeypatch):
    def boom(arguments):
        raise RuntimeError("trace store down")

    rounds = [_Resp("end_turn", [_Text("279 jobs.")])]
    qr, _ = _patch_turn(monkeypatch, rounds, _counts, on_event=boom)

    out = await qr.run_query_turn(uuid.uuid4(), uuid.uuid4(), "how many jobs?")
    assert out["answer"] == "279 jobs."  # observability is not worth an error


async def test_question_is_capped_in_the_trace(monkeypatch):
    rounds = [_Resp("end_turn", [_Text("ok")])]
    qr, written = _patch_turn(monkeypatch, rounds, _counts)

    await qr.run_query_turn(uuid.uuid4(), uuid.uuid4(), "x" * 5000)
    assert len(written[0]["payload_json"]["question"]) == 500


def test_trace_tools_are_not_in_the_query_agents_toolset():
    """The agent must not be able to read its own trace log: those rows quote questions
    from every session, including ones this turn never touched."""
    assert "list_agent_events" not in query_agent.TOOL_NAMES
    assert "record_agent_event" not in query_agent.TOOL_NAMES


# --------------------------------------------------------------------------- #
# Rollups
# --------------------------------------------------------------------------- #
def _t(**kw):
    base = {
        "status": "ok", "duration_ms": 1000, "ttft_ms": 300, "citations": 2,
        "tool_errors": 0, "routed_to_concierge": False,
        "spans": [{"kind": "tool", "name": "count_jobs", "ms": 50, "status": "ok", "rows": 1}],
        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 900,
                  "cache_creation_input_tokens": 0, "estimated_cost_usd": 0.001,
                  "naive_cost_usd": 0.002},
    }
    base.update(kw)
    return base


def test_summary_percentiles_use_nearest_rank():
    s = summarize_traces([_t(duration_ms=ms) for ms in (100, 200, 300, 400, 900)])
    assert s["turns"] == 5
    assert s["latency_ms"]["p50"] == 300 and s["latency_ms"]["max"] == 900
    assert s["latency_ms"]["p95"] == 900


def test_summary_rolls_up_tools_with_error_rate():
    traces = [
        _t(spans=[{"kind": "tool", "name": "count_jobs", "ms": 20, "status": "ok", "rows": 1},
                  {"kind": "tool", "name": "semantic_search_jobs", "ms": 300, "status": "error"}],
           tool_errors=1),
        _t(spans=[{"kind": "tool", "name": "count_jobs", "ms": 40, "status": "ok", "rows": 3}]),
    ]
    s = summarize_traces(traces)
    by_tool = {t["tool"]: t for t in s["tools"]}
    assert by_tool["count_jobs"]["calls"] == 2 and by_tool["count_jobs"]["error_pct"] == 0.0
    assert by_tool["count_jobs"]["avg_rows"] == 2.0
    assert by_tool["semantic_search_jobs"]["error_pct"] == 100.0
    assert s["tool_error_turns"] == 1
    assert s["tools"][0]["tool"] == "count_jobs"  # busiest first


def test_summary_flags_ungrounded_and_uncited_turns():
    s = summarize_traces([
        _t(spans=[]),                 # answered with no tool call at all
        _t(citations=0),              # pulled rows back, cited none of them
        _t(),                         # grounded and cited
    ])
    assert s["ungrounded_turns"] == 1
    assert s["uncited_turns"] == 1
    # An ungrounded turn is not also counted as uncited — they are different failures.
    assert s["turns"] == 3


def test_a_count_turn_citing_nothing_is_not_flagged_uncited():
    """count_jobs returns a number, not rows — there is nothing to cite, so the turn is
    correct. Flagging it would make the grounding tile cry wolf on the commonest question
    the agent answers."""
    s = summarize_traces([
        _t(citations=0,
           spans=[{"kind": "tool", "name": "count_jobs", "ms": 30, "status": "ok",
                   "rows": None, "bytes": 14}]),
    ])
    assert s["uncited_turns"] == 0 and s["ungrounded_turns"] == 0


def test_summary_cache_hit_and_cost():
    s = summarize_traces([_t(), _t()])
    assert s["cache_hit_pct"] == 90.0  # 1800 cached of 2000 billed input
    assert s["cost_usd"] == 0.002 and s["cost_per_turn_usd"] == 0.001
    assert s["savings_pct"] == 50.0


def test_summary_of_nothing_is_zeroed_not_an_error():
    s = summarize_traces([])
    assert s["turns"] == 0 and s["tools"] == []
    assert s["latency_ms"]["p50"] is None and s["cost_usd"] == 0.0
    assert s["cache_hit_pct"] == 0.0
