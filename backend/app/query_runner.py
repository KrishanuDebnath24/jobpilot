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

import json
import logging
import uuid

from jobpilot_core import query_agent
from jobpilot_core.embeddings import embed_texts
from jobpilot_core.llm import get_client
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


async def _default_call(user_id: uuid.UUID, service: str, tool: str, args: dict):
    return await _SERVICES[service](user_id, tool, args)


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
        """Embed the query locally, then run the vector search on db-mcp."""
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "semantic search needs a non-empty query"}
        vecs = embed_texts(self._settings, [query])
        if not vecs:
            return {"error": "could not embed the query"}
        limit = int(args.get("limit") or self._settings.query_semantic_limit)
        _, tool, _ = _ROUTES[name]
        return await self._call(self.user_id, "db", tool, {"embedding": vecs[0], "limit": limit})

    async def _list_qualified(self, args: dict) -> dict:
        """Read the dashboard join, filter by decision + cap (bounded list, not a count)."""
        limit = int(args.get("limit") or 25)
        decision = args.get("decision")
        res = await self._call(self.user_id, "db", "get_qualified_jobs", {"limit": 500})
        jobs = res.get("jobs", []) if isinstance(res, dict) else []
        if decision:
            jobs = [j for j in jobs if (j.get("qualification") or {}).get("decision") == decision]
        return {"jobs": jobs[:limit]}

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
    try:
        return await executor.execute(name, args)
    except Exception as exc:  # noqa: BLE001 - one bad tool call must not crash the turn
        logger.warning("query.tool %s failed: %s", name, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}, False


async def run_query_turn(user_id: uuid.UUID, session_id: uuid.UUID, question: str) -> dict:
    """Run one read-only Query turn. Routes the question to the right tool(s), grounds a
    cited answer, persists the exchange, and returns the answer + tool events +
    citations + a redirect hint for action requests."""
    settings = get_settings()
    history = (await mcp_client.db_mcp(
        user_id, "get_messages", {"chat_session_id": str(session_id)}))["messages"]
    executor = QueryExecutor(user_id)
    await _persist(user_id, session_id, "user", question)

    messages = _to_anthropic_messages(history) + [{"role": "user", "content": question}]
    client = get_client(settings)
    events_out: list[dict] = []
    citations: dict = {}
    resp = None

    try:
        for _ in range(settings.query_max_tool_iterations):
            resp = await client.messages.create(
                model=settings.query_model,
                max_tokens=settings.query_max_tokens,
                system=query_agent.system_blocks(),
                tools=query_agent.TOOLS,
                messages=messages,
                thinking={"type": "disabled"},  # interactive: low latency, no scratchpad
            )
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                break
            tool_results = []
            for tu in tool_uses:
                result, is_data = await _safe_execute(executor, tu.name, tu.input or {})
                events_out.append({"tool": tu.name,
                                   "status": "error" if isinstance(result, dict) and result.get("error") else "ok"})
                _citations_from(tu.name, result if isinstance(result, dict) else {}, citations)
                content = (query_agent.wrap_retrieved(tu.name, json.dumps(result))
                           if is_data else json.dumps(result))
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
            messages.append({"role": "user", "content": tool_results})
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass

    answer = "".join(b.text for b in (resp.content if resp else []) if getattr(b, "type", None) == "text")
    cite_list = list(citations.values())
    routed = query_agent.looks_like_action(question) and not events_out
    await _persist(user_id, session_id, "assistant", answer,
                   {"events": events_out, "citations": cite_list} if (events_out or cite_list) else None)
    logger.info("query.turn tools=%d citations=%d", len(events_out), len(cite_list))
    return {"answer": answer, "events": events_out, "citations": cite_list,
            "routed_to_concierge": routed}
