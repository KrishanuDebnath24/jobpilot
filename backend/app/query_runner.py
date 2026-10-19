"""Query agent loop + read-only tool executor (Phase 7, 7c/7d).

The Query agent answers; it never acts. This module holds:

  * ``QueryExecutor`` — read-only dispatch under the trust boundary. ``user_id`` is
    bound from the session (never from tool args); it strips any ``user_id`` an
    argument carries. There are NO write tools — the whole executor can only read. For
    the semantic tools it embeds the natural-language ``query`` locally (sentence-
    transformers) before calling the vector search on db-mcp.
  * ``run_query_turn`` — the interactive Haiku tool-use loop, stable system prompt +
    tool defs cached, retrieved content wrapped as ``<data>``, answers grounded and
    cited. Conversation persisted through db-mcp (its own session, separate from the
    Concierge's).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from jobpilot_core import query_agent
from jobpilot_core.embeddings import embed_texts
from jobpilot_core.llm import LLMUsage, get_client
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.query")

# tool -> (service, mcp_tool, arg_mapper|None). Reads only.
_ROUTES: dict[str, tuple[str, str, object]] = {
    "count_jobs": ("db", "count_jobs", None),
    "count_qualifications": ("db", "count_qualifications", None),
    "job_stats": ("db", "job_stats", None),
    "get_suggestions": ("db", "get_suggestions", None),
    "get_search_config": ("config", "get_search_config", None),
    "get_qualifier_config": ("config", "get_qualifier_config", None),
    "semantic_search_jobs": ("db", "semantic_search_jobs", None),
    "semantic_search_analyses": ("db", "semantic_search_analyses", None),
}

_SERVICES = {
    "db": mcp_client.db_mcp,
    "config": mcp_client.config_mcp,
    "pipeline": mcp_client.pipeline_mcp,
}

# Which metadata filters each semantic tool forwards to db-mcp. An allowlist rather than a
# passthrough: ``_semantic`` builds the MCP args by hand (``query`` becomes ``embedding``),
# so anything not named here never reaches the query — including a filter the model invented
# for the wrong tool. ``company`` on analyses resolves through the analysed job.
_SEMANTIC_FILTERS: dict[str, tuple[str, ...]] = {
    "semantic_search_jobs": (
        "company", "decision", "since_days", "employment_type", "seniority"),
    "semantic_search_analyses": ("company", "since_days", "seniority"),
}


async def _default_call(user_id: uuid.UUID, service: str, tool: str, args: dict):
    return await _SERVICES[service](user_id, tool, args)


def _job_brief(job: dict) -> dict:
    """The fields an ANSWER or a citation can actually use, and nothing else.

    ``get_qualified_jobs`` serialises every column via ``job_to_dict`` — including the
    entire job description — because it is the dashboard's query and the dashboard renders
    all of it. For the agent that is ~1,000 tokens a row, re-sent uncached on every later
    round of the turn, to answer questions whose real content is a title and a link.

    ``apply_url`` is carried alongside ``url`` because most connectors store a tracking
    redirect in ``url``; the terminal application page is the link worth quoting when the
    two differ."""
    qualification = job.get("qualification") or {}
    return {
        "id": job.get("id"),
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "url": job.get("url"),
        "apply_url": job.get("apply_url"),
        "score": qualification.get("score"),
        "decision": qualification.get("decision"),
    }


class QueryExecutor:
    """Dispatch a read-only Query tool call under the trust boundary. ``user_id`` is
    fixed from the session; no tool can change it and none can write."""

    def __init__(self, user_id: uuid.UUID, call=None):
        self.user_id = user_id
        self._call = call or _default_call
        self._settings = get_settings()

    async def execute(self, name: str, args: dict) -> tuple[dict, bool]:
        """Returns ``(result, is_data)``. ``is_data`` marks untrusted retrieved content
        that must be wrapped in <data> before the model sees it."""
        args = dict(args or {})
        args.pop("user_id", None)  # trust boundary: identity never comes from tool args
        if name not in query_agent.TOOL_NAMES:
            return {"error": f"unknown tool {name!r}"}, False
        is_data = name in query_agent.DATA_TOOLS

        if name == "get_work_status":
            return await self._work_status(), False
        if name == "list_qualified_jobs":
            return await self._list_qualified(args), True
        if name == "list_cv_drafts":
            return await self._list_cv_drafts(args), True
        if name in query_agent.SEMANTIC_TOOLS:
            return await self._semantic(name, args), is_data

        service, tool, mapper = _ROUTES[name]
        mcp_args = mapper(args) if mapper else args
        result = await self._call(self.user_id, service, tool, mcp_args)
        return (result if isinstance(result, dict) else {"result": result}), is_data

    async def _semantic(self, name: str, args: dict) -> dict:
        """Embed the query locally, then run the vector search on db-mcp.

        ``embed_texts`` is synchronous CPU work (a torch forward pass on the local
        backend), so it runs in a worker thread: awaited inline it would hold the event
        loop for its whole duration, stalling every other in-flight request on this
        process — and it would serialise two semantic tools fanned out in one round."""
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "semantic search needs a non-empty query"}
        vecs = await asyncio.to_thread(embed_texts, self._settings, [query])
        if not vecs:
            return {"error": "could not embed the query"}
        limit = int(args.get("limit") or self._settings.query_semantic_limit)
        # Forward only the filters this tool declares, and only when actually set. Strings
        # are stripped first: a blank or whitespace-only company would otherwise become
        # `ILIKE '%  %'` — a filter that silently matches almost nothing, which reads to the
        # user as "you have no Stripe roles" rather than as a bad argument.
        filters: dict = {}
        for key in _SEMANTIC_FILTERS.get(name, ()):
            value = args.get(key)
            if isinstance(value, str):
                value = value.strip()
            if value not in (None, ""):
                filters[key] = value
        _, tool, _ = _ROUTES[name]
        result = await self._call(
            self.user_id, "db", tool, {"embedding": vecs[0], "limit": limit, **filters})
        return await self._explain_empty(result, filters)

    async def _explain_empty(self, result: dict, filters: dict) -> dict:
        """Turn an empty company-filtered result into a recoverable one.

        Zero rows is otherwise indistinguishable from "you genuinely have none", so a
        mis-guessed employer name gets reported as fact. On a miss — and ONLY on a miss, so
        the happy path costs nothing — attach the employers that actually exist, and the
        filters that were applied: if the name IS in the list, the emptiness came from
        another filter, and the model needs to see that rather than blame the name."""
        if not isinstance(result, dict) or not filters.get("company"):
            return result
        rows = next((v for v in result.values() if isinstance(v, list)), None)
        if rows:  # a hit, or a shape we don't recognise — leave it alone
            return result
        try:
            stats = await self._call(self.user_id, "db", "job_stats", {})
            companies = [c.get("label") for c in (stats.get("by_company") or [])
                         if isinstance(c, dict) and c.get("label")]
        except Exception as exc:  # noqa: BLE001 - the empty result is still a valid answer
            logger.warning("query.company_recovery failed: %s", exc)
            return result
        return {**result, "filters_applied": dict(filters), "companies_in_pool": companies}

    async def _list_qualified(self, args: dict) -> dict:
        """Read the dashboard join, filter by decision + cap, and project each row down to
        the answerable fields (see :func:`_job_brief`) — the shared endpoint's full row is
        the dashboard's shape, not an answer's, and paying for it here bought nothing."""
        limit = int(args.get("limit") or 25)
        decision = args.get("decision")
        res = await self._call(self.user_id, "db", "get_qualified_jobs", {"limit": 500})
        jobs = res.get("jobs", []) if isinstance(res, dict) else []
        if decision:
            jobs = [j for j in jobs if (j.get("qualification") or {}).get("decision") == decision]
        return {"jobs": [_job_brief(j) for j in jobs[:limit]]}

    async def _list_cv_drafts(self, args: dict) -> dict:
        """Tailored-CV drafts (Phase 6.5) with before/after scores and the EXACT score
        DELTA (after − before), sorted by delta. 'Which of my CVs improved the most?' is
        this sort — never a similarity search. A read-only pull of the same numbers the
        Concierge pushes after a re-qualify; the Query agent never writes. Mirrors the
        Concierge's delta computation (round 4dp, ±0.0001 improved/worse threshold), but
        keeps not-yet-requalified drafts (delta null, sorted last) so status questions
        ('how many are waiting for review?') are answerable too."""
        status = args.get("status")
        sort = (args.get("sort") or "delta_desc").lower()
        limit = int(args.get("limit") or 50)
        res = await self._call(self.user_id, "db", "get_cv_drafts",
                               {"status": status} if status else {})
        drafts = res.get("drafts", []) if isinstance(res, dict) else []
        jobs_res = await self._call(self.user_id, "db", "get_qualified_jobs", {"limit": 5000})
        jobs = jobs_res.get("jobs", []) if isinstance(jobs_res, dict) else []
        by_id = {j.get("id"): j for j in jobs if isinstance(j, dict)}

        rows: list[dict] = []
        improved = worse = unchanged = requalified = 0
        by_status: dict[str, int] = {}
        for d in drafts:
            st = d.get("status")
            by_status[st] = by_status.get(st, 0) + 1
            before, after = d.get("before_score"), d.get("after_score")
            delta = round(after - before, 4) if (before is not None and after is not None) else None
            if after is not None:
                requalified += 1
            if delta is not None:
                improved += delta > 0.0001
                worse += delta < -0.0001
                unchanged += abs(delta) <= 0.0001
            job = by_id.get(d.get("job_id")) or {}
            rows.append({"job_id": d.get("job_id"), "title": job.get("title"),
                         "company": job.get("company"), "url": job.get("url"),
                         "status": st, "before_score": before, "after_score": after,
                         "delta": delta})
        # EXACT sort: real deltas first (desc unless delta_asc), not-yet-requalified last
        scored = [r for r in rows if r["delta"] is not None]
        unscored = [r for r in rows if r["delta"] is None]
        scored.sort(key=lambda r: r["delta"], reverse=(sort != "delta_asc"))
        summary = {"total": len(rows), "requalified": requalified, "improved": improved,
                   "worse": worse, "unchanged": unchanged, "by_status": by_status}
        return {"drafts": (scored + unscored)[:limit], "summary": summary}

    async def _work_status(self) -> dict:
        """Live pipeline state (pipeline-mcp) + the last run per stage (db-mcp). Trusted
        structural facts — the ONLY grounding for status questions."""
        status = await self._call(self.user_id, "pipeline", "get_pipeline_status", {})
        stages = await self._call(self.user_id, "db", "latest_stage_runs", {})
        return {
            "active": bool(status.get("active")) if isinstance(status, dict) else False,
            "pending_gate": status.get("pending_gate") if isinstance(status, dict) else None,
            "pipeline": status.get("pipeline") if isinstance(status, dict) else None,
            "latest_stage_runs": stages.get("stages") if isinstance(stages, dict) else {},
        }


# --------------------------------------------------------------------------- #
# Citations — surface the specific jobs/runs an answer rests on (for UI links)
# --------------------------------------------------------------------------- #
def _citations_from(name: str, result: dict, sink: dict) -> None:
    if not isinstance(result, dict):
        return
    for job in result.get("jobs", []) or []:
        if isinstance(job, dict) and job.get("id"):
            sink[("job", job["id"])] = {
                "type": "job", "id": job["id"], "title": job.get("title"),
                "company": job.get("company"), "url": job.get("url"),
                "similarity": job.get("similarity"),
            }
    for draft in result.get("drafts", []) or []:  # cv_drafts cite their target job
        if isinstance(draft, dict) and draft.get("job_id"):
            sink[("job", draft["job_id"])] = {
                "type": "job", "id": draft["job_id"], "title": draft.get("title"),
                "company": draft.get("company"), "url": draft.get("url"),
            }
    for stage, run in (result.get("latest_stage_runs") or {}).items():
        if isinstance(run, dict):
            sink[("run", stage)] = {
                "type": "run", "stage": stage, "status": run.get("status"),
                "finished_at": run.get("finished_at"), "pipeline_id": run.get("pipeline_id"),
            }


# --------------------------------------------------------------------------- #
# Turn loop
# --------------------------------------------------------------------------- #
def _to_anthropic_messages(history: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in history:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            out.append({"role": m["role"], "content": m["content"]})
    return out


async def _persist(user_id, session_id, role, content, tool_calls_json=None):
    await mcp_client.db_mcp(user_id, "append_message", {
        "chat_session_id": str(session_id), "role": role,
        "content": content, "tool_calls_json": tool_calls_json})


async def _safe_execute(executor: "QueryExecutor", name: str, args: dict):
    """Never raises — one bad tool call must not crash the turn. Returns the elapsed ms
    alongside the result: a round's tools run concurrently, so only the call itself knows
    how long it took, and wall-clock around the ``gather`` would credit them all with the
    slowest one's time."""
    started = time.perf_counter()
    try:
        result, is_data = await executor.execute(name, args)
    except Exception as exc:  # noqa: BLE001
        logger.warning("query.tool %s failed: %s", name, exc)
        result, is_data = {"error": f"{type(exc).__name__}: {exc}"}, False
    return result, is_data, round((time.perf_counter() - started) * 1000, 1)


# --------------------------------------------------------------------------- #
# Tracing — one row per turn, so the agent's behaviour is inspectable after the fact
# --------------------------------------------------------------------------- #
TRACE_EVENT = "query_turn"
_ARG_CAP = 300      # a captured tool argument is a label, not a copy of the input
_ANSWER_PREVIEW = 240


def _arg_digest(args: dict) -> str:
    """The model's own tool input, capped. Kept because the useful question about a slow
    or empty turn is almost always *what did it search for* — a semantic query the model
    invented is invisible in the tool name alone."""
    try:
        blob = json.dumps(args, default=str, sort_keys=True)
    except Exception:  # noqa: BLE001
        blob = str(args)
    return blob if len(blob) <= _ARG_CAP else blob[:_ARG_CAP] + "…"


def _result_shape(result) -> dict:
    """Size + row count of what a tool returned. Distinguishes 'the search found nothing'
    from 'the search returned 8 rows the model then ignored' — same tool, same status."""
    try:
        size = len(json.dumps(result, default=str))
    except Exception:  # noqa: BLE001
        size = None
    rows = None
    if isinstance(result, dict):
        for key in ("jobs", "drafts", "analyses", "suggestions", "events"):
            if isinstance(result.get(key), list):
                rows = len(result[key])
                break
    return {"bytes": size, "rows": rows}


class _Trace:
    """One Query turn, timed.

    Every span carries an offset from turn start (``t``) as well as a duration, which is
    what lets the dashboard draw a waterfall rather than a list: the tools of one round
    share a ``t`` because they genuinely run at once.
    """

    def __init__(self, session_id: uuid.UUID, question: str, model: str) -> None:
        self._t0 = time.perf_counter()
        self.started_at = datetime.now(timezone.utc)
        self.session_id = str(session_id)
        self.question = question[:500]
        self.model = model
        self.usage = LLMUsage(model=model)
        self.spans: list[dict] = []
        self.rounds = 0
        self.ttft_ms: float | None = None
        self.stop_reason: str | None = None
        self.error: str | None = None

    def now_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def add_usage(self, resp) -> dict:
        """Fold one round's token usage into the turn total, returning that round's own
        counts for its span. ``usage`` is absent on a stubbed response, so every field is
        read defensively — a missing meter must not cost us the trace."""
        u = getattr(resp, "usage", None)
        got = {k: int(getattr(u, k, 0) or 0) for k in (
            "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens")}
        self.usage.add(got["input_tokens"], got["output_tokens"],
                       cache_read=got["cache_read_input_tokens"],
                       cache_creation=got["cache_creation_input_tokens"])
        return got

    def payload(self, *, answer: str, citations: list, routed: bool) -> dict:
        tools = [s for s in self.spans if s["kind"] == "tool"]
        return {
            "v": 1,
            "session_id": self.session_id,
            "question": self.question,
            "model": self.model,
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.now_ms(),
            "ttft_ms": self.ttft_ms,
            "rounds": self.rounds,
            "stop_reason": self.stop_reason,
            "status": "error" if self.error else "ok",
            "error": self.error,
            "answer_chars": len(answer),
            "answer_preview": answer[:_ANSWER_PREVIEW],
            "citations": len(citations),
            "routed_to_concierge": routed,
            "tools": [s["name"] for s in tools],
            "tool_errors": sum(1 for s in tools if s.get("status") == "error"),
            "usage": self.usage.as_dict(),
            "spans": self.spans,
        }


async def _record_trace(user_id: uuid.UUID, payload: dict, keep: int) -> None:
    """Write the trace, and never let that failure become the user's failure — the answer
    is already streamed and persisted by this point; observability is not worth an error."""
    try:
        await mcp_client.db_mcp(user_id, "record_agent_event", {
            "type": TRACE_EVENT, "payload_json": payload, "keep": keep})
    except Exception as exc:  # noqa: BLE001
        logger.warning("query.trace write failed: %s", exc)


async def stream_query_turn(
    user_id: uuid.UUID, session_id: uuid.UUID, question: str
) -> AsyncIterator[dict]:
    """Run one read-only Query turn, yielding progress as it happens.

    This is the turn implementation; :func:`run_query_turn` is a thin drain of it, so the
    streaming and one-shot paths can never drift apart. Yields, in order:

      * ``{"kind": "token", "text": …}``  — answer text as the model produces it
      * ``{"kind": "tool",  "tool": …, "status": …}`` — one per tool, as it completes
      * ``{"kind": "done",  "answer": …, "events": …, "citations": …, …}`` — exactly once

    The answer is the text ACCUMULATED across rounds, not just the final round's: with a
    live UI the user has already seen any preamble the model wrote before calling a tool,
    so dropping it would make the persisted transcript disagree with the screen.
    """
    settings = get_settings()
    history = (await mcp_client.db_mcp(
        user_id, "get_messages", {"chat_session_id": str(session_id)}))["messages"]
    executor = QueryExecutor(user_id)
    await _persist(user_id, session_id, "user", question)

    messages = _to_anthropic_messages(history) + [{"role": "user", "content": question}]
    client = get_client(settings)
    events_out: list[dict] = []
    citations: dict = {}
    answer_parts: list[str] = []
    trace = _Trace(session_id, question, settings.query_model)

    try:
        for _ in range(settings.query_max_tool_iterations):
            round_t = trace.now_ms()
            first_token_at: float | None = None
            # Streamed rather than awaited whole: the text of the final round reaches the
            # browser token by token, which is the difference between a ~400ms first paint
            # and a dead spinner for the length of the turn.
            async with client.messages.stream(
                model=settings.query_model,
                max_tokens=settings.query_max_tokens,
                system=query_agent.system_blocks(),
                tools=query_agent.TOOLS,
                messages=messages,
                thinking={"type": "disabled"},  # interactive: low latency, no scratchpad
            ) as stream:
                async for chunk in stream.text_stream:
                    if first_token_at is None:
                        first_token_at = trace.now_ms()
                    answer_parts.append(chunk)
                    yield {"kind": "token", "text": chunk}
                resp = await stream.get_final_message()

            trace.rounds += 1
            trace.stop_reason = getattr(resp, "stop_reason", None)
            if trace.ttft_ms is None:
                trace.ttft_ms = first_token_at  # of the TURN: what the user waited for
            trace.spans.append({
                "kind": "llm", "name": f"round {trace.rounds}", "t": round_t,
                "ms": round(trace.now_ms() - round_t, 1),
                "ttft_ms": (round(first_token_at - round_t, 1)
                            if first_token_at is not None else None),
                "stop_reason": trace.stop_reason, "status": "ok",
                **trace.add_usage(resp),
            })
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                break
            # Fan the round's tools out concurrently — they're independent reads, so N
            # tools cost one round trip, not N. ``_safe_execute`` never raises, so gather
            # always yields a full row of (result, is_data) and needs no return_exceptions.
            tools_t = trace.now_ms()  # shared start: the round's tools go out together
            outcomes = await asyncio.gather(
                *(_safe_execute(executor, tu.name, tu.input or {}) for tu in tool_uses)
            )
            # Fold back in REQUEST order (gather preserves it), not completion order, so
            # events + citations stay deterministic regardless of which reply lands first.
            tool_results = []
            for tu, (result, is_data, tool_ms) in zip(tool_uses, outcomes):
                event = {"tool": tu.name,
                         "status": "error" if isinstance(result, dict) and result.get("error") else "ok"}
                events_out.append(event)
                yield {"kind": "tool", **event}
                trace.spans.append({
                    "kind": "tool", "name": tu.name, "t": tools_t, "ms": tool_ms,
                    "status": event["status"], "args": _arg_digest(tu.input or {}),
                    "error": result.get("error") if event["status"] == "error" else None,
                    **_result_shape(result),
                })
                _citations_from(tu.name, result if isinstance(result, dict) else {}, citations)
                content = (query_agent.wrap_retrieved(tu.name, json.dumps(result))
                           if is_data else json.dumps(result))
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
            messages.append({"role": "user", "content": tool_results})
    except Exception as exc:  # noqa: BLE001 - a failed turn is the one worth tracing
        trace.error = f"{type(exc).__name__}: {exc}"
        if settings.query_trace_enabled:
            await _record_trace(user_id, trace.payload(
                answer="".join(answer_parts), citations=list(citations.values()),
                routed=False), settings.query_trace_keep)
        raise
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass

    answer = "".join(answer_parts)
    cite_list = list(citations.values())
    routed = query_agent.looks_like_action(question) and not events_out
    await _persist(user_id, session_id, "assistant", answer,
                   {"events": events_out, "citations": cite_list} if (events_out or cite_list) else None)
    if settings.query_trace_enabled:
        await _record_trace(user_id, trace.payload(answer=answer, citations=cite_list, routed=routed),
                            settings.query_trace_keep)
    logger.info("query.turn tools=%d citations=%d ms=%.0f cost=$%.5f",
                len(events_out), len(cite_list), trace.now_ms(), trace.usage.cost_usd)
    yield {"kind": "done", "answer": answer, "events": events_out,
           "citations": cite_list, "routed_to_concierge": routed}


async def run_query_turn(user_id: uuid.UUID, session_id: uuid.UUID, question: str) -> dict:
    """Run one read-only Query turn and return it whole (the non-streaming endpoint).

    Drains :func:`stream_query_turn` and keeps its terminal ``done`` payload, so both
    entry points share one implementation of routing, grounding, and persistence."""
    final: dict = {}
    async for event in stream_query_turn(user_id, session_id, question):
        if event.get("kind") == "done":
            final = event
    return {"answer": final.get("answer", ""),
            "events": final.get("events", []),
            "citations": final.get("citations", []),
            "routed_to_concierge": final.get("routed_to_concierge", False)}
