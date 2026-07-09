"""Phase 8a — observability & tracing (OpenTelemetry local).

Deterministic, offline (no DB, no LLM, no network): drive the tracing module through an
in-memory span exporter and prove the mechanism the "show me" needs —

  * a CONNECTED trace: stage → mcp → llm spans nest as parent→child;
  * per-call token/cost visible on the llm span (from LLMUsage), and correlation ids
    (user_id / pipeline_id / stage) on every span;
  * NO PII: long string attributes are truncated (ids/labels only), and the log filter
    injects only ids/counts — never content;
  * graceful no-op when tracing is disabled (and, by extension, when OTel is absent).
"""

from __future__ import annotations

import logging

import pytest
from jobpilot_core import tracing
from jobpilot_core.llm import LLMUsage
from jobpilot_core.settings import Settings
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture()
def spans():
    """Init tracing with an in-memory exporter; yield the exporter; tear down."""
    exporter = InMemorySpanExporter()
    assert tracing.init_tracing(Settings(tracing_enabled=True), exporter=exporter)
    try:
        yield exporter
    finally:
        tracing.shutdown()


def _by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


# --------------------------------------------------------------------------- #
# Connected trace + per-call token/cost + correlation ids
# --------------------------------------------------------------------------- #
def test_stage_mcp_llm_spans_form_a_connected_trace(spans):
    with tracing.span("stage.qualify", stage="qualify", user_id="u1", pipeline_id="p1"):
        with tracing.span("mcp.tool", tool="count_jobs", service="db"):
            pass
        with tracing.span("llm.call", model="claude-haiku-4-5", kind="tool") as sp:
            sp.record_llm(LLMUsage(model="claude-haiku-4-5", input_tokens=100, output_tokens=20))

    finished = _by_name(spans)
    assert {"stage.qualify", "mcp.tool", "llm.call"} <= set(finished)
    stage, mcp, llm = finished["stage.qualify"], finished["mcp.tool"], finished["llm.call"]

    # one connected trace: all share a trace id; mcp + llm are children of the stage span
    assert stage.context.trace_id == mcp.context.trace_id == llm.context.trace_id
    assert mcp.parent.span_id == stage.context.span_id
    assert llm.parent.span_id == stage.context.span_id

    # per-call token/cost is on the llm span; the mcp span carries the tool name
    assert llm.attributes["input_tokens"] == 100 and llm.attributes["output_tokens"] == 20
    assert llm.attributes["est_cost_usd"] > 0 and llm.attributes["model"] == "claude-haiku-4-5"
    assert mcp.attributes["tool"] == "count_jobs"

    # correlation ids propagate onto the stage span (and its children inherit via context)
    assert stage.attributes["user_id"] == "u1" and stage.attributes["pipeline_id"] == "p1"
    assert stage.attributes["stage"] == "qualify" and stage.attributes["outcome"] == "ok"


async def test_traced_stage_decorator_sets_correlation_and_lifts_counts(spans):
    @tracing.traced_stage("scout")
    async def run(user_id, *, pipeline_id=None):
        # inside the stage, correlation ids are live (so logs during the run carry them)
        c = tracing.get_correlation()
        assert c["user_id"] == "u9" and c["pipeline_id"] == "p9" and c["stage"] == "scout"
        return {"status": "success", "new": 7, "deduped": 2}

    out = await run("u9", pipeline_id="p9")
    assert out["new"] == 7
    # correlation is reset after the run (no leakage across requests)
    assert tracing.get_correlation() == {}
    span = _by_name(spans)["stage.scout"]
    assert span.attributes["new"] == 7 and span.attributes["status"] == "success"
    assert span.attributes["user_id"] == "u9"


def test_error_outcome_is_recorded_and_reraised(spans):
    with pytest.raises(ValueError):
        with tracing.span("stage.boom", stage="x"):
            raise ValueError("kaboom")
    span = _by_name(spans)["stage.boom"]
    assert span.attributes["outcome"] == "error" and span.attributes["error.type"] == "ValueError"


# --------------------------------------------------------------------------- #
# PII boundary — attributes are ids/counts only; long strings truncated
# --------------------------------------------------------------------------- #
def test_string_attributes_are_truncated_as_a_pii_safety_net(spans):
    resume_like = "SECRET RESUME TEXT " * 100  # ~1900 chars of "content"
    with tracing.span("stage.x", note=resume_like):
        pass
    val = _by_name(spans)["stage.x"].attributes["note"]
    assert len(val) <= tracing._MAX_ATTR_LEN  # never carries a full document


def test_correlation_filter_injects_ids_not_content():
    filt = tracing.CorrelationFilter()
    token = tracing.set_correlation(user_id="u1", pipeline_id="p1", stage="qualify")
    try:
        record = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", None, None)
        assert filt.filter(record)
        assert record.user_id == "u1" and record.pipeline_id == "p1" and record.stage == "qualify"
    finally:
        tracing.reset_correlation(token)
    # cleared context → placeholders, never stale ids
    record2 = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", None, None)
    filt.filter(record2)
    assert record2.user_id == "-" and record2.stage == "-"


# --------------------------------------------------------------------------- #
# Graceful no-op when disabled (mirrors the OTel-absent path)
# --------------------------------------------------------------------------- #
def test_disabled_tracing_is_a_noop():
    tracing.shutdown()
    assert tracing.init_tracing(Settings(tracing_enabled=False)) is False
    assert tracing.is_enabled() is False
    with tracing.span("stage.x", a=1) as sp:  # no exporter, no provider — must not raise
        sp.set(b=2).record_llm(LLMUsage(model="m"))
