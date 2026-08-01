"""Concierge agentic loop + tool executor (Phase 6, 6b/6c/6d).

The Concierge reaches data/config/pipeline ONLY through the MCP servers, all
user-scoped. This module holds:

  * ``ConciergeExecutor`` — the security boundary. It binds ``user_id`` from the
    session (never from tool arguments), strips any ``user_id`` a tool tries to pass,
    and gates every consequential/destructive tool behind an explicit confirmation.
    Even if injected text convinced the model to call ``delete_user_data``, the
    executor blocks it without a real user yes. This is unit-tested directly.
  * ``run_turn`` — the interactive tool-use loop against a Sonnet tier, with the stable
    system prompt + tool defs cached, retrieved content wrapped as ``<data>``, and the
    conversation persisted through db-mcp. Streams tool-call progress.
"""

from __future__ import annotations

import json
import logging
import uuid

from jobpilot_core import concierge, cv_digest, cv_edit, cv_fields
from jobpilot_core.llm import get_client
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.concierge")

# Per-job CV Q&A/edit tools, handled locally (reads + deterministic edits + one focused
# reword). They target ONE job resolved by role+company or selection — never an id.
_CV_QUERY_LOCAL = {"explain_job", "show_cv", "edit_cv", "reword_cv"}

# Words to ignore when matching a free-text "role at company" to a job.
_JOBREF_STOP = {"at", "the", "a", "an", "role", "roles", "job", "jobs", "position", "for",
                "to", "cv", "my", "this", "that", "of", "in", "with"}


def _match_jobs(jobs: list[dict], ref: str) -> list[dict]:
    """Jobs whose title+company contains every significant word of ``ref`` (a free-text
    'Backend Engineer at Stripe'). Deterministic, so resolving a job by role+company costs
    no tokens and never needs a job id from the user."""
    toks = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in (ref or "").lower()).split()
            if len(t) > 1 and t not in _JOBREF_STOP]
    if not toks:
        return []
    out = []
    for j in jobs:
        hay = f"{j.get('title') or ''} {j.get('company') or ''}".lower()
        if all(t in hay for t in toks):
            out.append(j)
    return out

# tool -> (service, mcp_tool, arg_mapper|None, wrap_result_as_data)
_ROUTES: dict[str, tuple[str, str, object, bool]] = {
    "get_dashboard": ("db", "get_qualified_jobs", lambda a: {"limit": int(a.get("limit") or 200)}, True),
    "get_suggestions": ("db", "get_suggestions", None, True),
    "get_search_config": ("config", "get_search_config", None, True),
    "update_search_config": ("config", "update_search_config", None, False),
    "get_qualifier_config": ("config", "get_qualifier_config", None, True),
    "update_qualifier_config": ("config", "update_qualifier_config", None, False),
    "get_gates": ("config", "get_gate_config", None, True),
    "update_gate": ("config", "update_gate_config", None, False),
    "get_pipeline_status": ("pipeline", "get_pipeline_status", None, True),
    "run_pipeline": ("pipeline", "run_full_pipeline", lambda a: {}, False),
    # trigger_stage is handled locally (see _run_stage): qualify/analyze/enhance run
    # synchronously in the turn; scout stays on the worker (a crawl can run for minutes).
    "approve_gate": ("pipeline", "approve_gate", None, False),
    "skip_gate": ("pipeline", "skip_gate", None, False),
    "list_cv_drafts": ("db", "get_cv_drafts", None, True),  # Phase 6.5: the user's own drafts (data)
    "delete_jobs": ("db", "delete_jobs", lambda a: {"job_ids": a.get("job_ids") or []}, False),
    "delete_disqualified_jobs": ("db", "delete_disqualified_jobs", lambda a: {}, False),
    "delete_user_data": ("db", "delete_user_data", lambda a: {}, False),
}
_LOCAL = {"prepare_resume_apply", "apply_resume"}
# Phase 6.5 CV-flow tools handled locally (enqueue a task or a scoped db-mcp write).
_CV_LOCAL = {"tailor_cvs", "prepare_cvs", "export_cvs", "requalify_cvs", "approve_cv_drafts",
             "reject_cv_drafts", "cv_improvement_report"}
# Job-scoped tools whose target can be the user's current dashboard selection: when the
# model passes use_selected=true (and no explicit job_ids), the executor substitutes the
# selection it was constructed with — the model never has to echo opaque job IDs.
_SELECTION_SCOPED = {"tailor_cvs", "prepare_cvs", "delete_jobs", "trigger_stage"}

_SERVICES = {
    "db": mcp_client.db_mcp,
    "config": mcp_client.config_mcp,
    "pipeline": mcp_client.pipeline_mcp,
    "resume": mcp_client.resume_mcp,
}


async def _default_call(user_id: uuid.UUID, service: str, tool: str, args: dict) -> dict:
    return await _SERVICES[service](user_id, tool, args)


class ConciergeExecutor:
    """Dispatch a Concierge tool call under the trust boundary. ``user_id`` is fixed at
    construction from the session; a tool argument can never change it."""

    def __init__(self, user_id: uuid.UUID, call=None, selected_job_ids: list[str] | None = None):
        self.user_id = user_id
        self._call = call or _default_call
        # The user's current dashboard selection, from the authenticated request (trusted
        # context, not retrieved data). Job-scoped tools resolve "selected" against this.
        self.selected_job_ids = list(selected_job_ids or [])

    async def _resolve_targets(self, name: str, args: dict) -> tuple[dict, dict | None]:
        """Resolve which jobs a selection-scoped action targets — from explicit ``job_ids``,
        the dashboard ``use_selected`` set, or a free-text ``job_ref`` (role+company) — so the
        user never has to give an id. Returns ``(args_with_job_ids, error)``: on a job_ref that
        matches nothing, ``error`` is a helpful note; ambiguity is left to the caller (a batch
        action legitimately spans several matches, and the confirm prompt shows the count)."""
        if name not in _SELECTION_SCOPED or args.get("job_ids"):
            return args, None
        if args.get("use_selected"):
            return {**args, "job_ids": list(self.selected_job_ids)}, None
        ref = (args.get("job_ref") or "").strip()
        if ref:
            matches = _match_jobs(await self._all_jobs(), ref)
            if not matches:
                return args, {"status": "no_match",
                              "note": f"No job matches '{ref}'. Try the exact role and company."}
            return {**args, "job_ids": [m["id"] for m in matches]}, None
        return args, None

    async def execute(self, name: str, args: dict, confirmed: bool = False) -> tuple[dict, bool]:
        """Returns ``(result, wrap_as_data)``. A consequential/destructive tool called
        without ``confirmed`` returns a ``confirmation_required`` result (carrying the
        selection-resolved ``resolved_args``) and performs NO action."""
        args = dict(args or {})
        args.pop("user_id", None)  # trust boundary: identity never comes from tool args
        if name not in concierge.TOOL_NAMES:
            return {"error": f"unknown tool {name!r}"}, False
        # Per-job CV Q&A/edit tools resolve their own target (role+company or selection) and
        # need no confirmation (reads, reversible edits) — handle before the paid-action gate.
        if name in _CV_QUERY_LOCAL:
            return await self._cv_query(name, args)
        # Resolve the target jobs (selection or role+company) BEFORE authorization so the
        # confirm prompt names the real target and the held action fires on exactly those ids.
        args, ref_err = await self._resolve_targets(name, args)
        if ref_err:
            return ref_err, False
        # "Act on selected" but nothing is selected → don't confirm-and-no-op; tell the
        # agent so it can ask the user to pick jobs first.
        if name in _SELECTION_SCOPED and args.get("use_selected") and not args.get("job_ids"):
            return {"status": "no_selection",
                    "note": "No jobs are selected — ask the user to tick jobs in the table first."}, False
        if concierge.authorize(name, confirmed) == "confirm":
            return {"status": "confirmation_required",
                    "prompt": concierge.confirmation_prompt(name, args),
                    "resolved_args": args}, False

        if name == "prepare_resume_apply":
            from .apply_runner import prepare_apply
            return await prepare_apply(self.user_id), True
        if name == "apply_resume":
            from .apply_runner import apply_to_resume
            return await apply_to_resume(self.user_id, args.get("confirmed_skills")), False
        if name == "trigger_stage":
            return await self._run_stage(args)
        if name in _CV_LOCAL:
            return await self._cv(name, args)

        service, tool, mapper, is_data = _ROUTES[name]
        mcp_args = mapper(args) if mapper else args
        result = await self._call(self.user_id, service, tool, mcp_args)
        return (result if isinstance(result, dict) else {"result": result}), is_data

    async def _resolve_cv_ids(self, args: dict) -> list[str]:
        if args.get("draft_ids"):
            return list(args["draft_ids"])
        if args.get("all"):
            drafts = (await self._call(self.user_id, "db", "get_cv_drafts", {"status": "draft_ready"}))["drafts"]
            return [d["id"] for d in drafts]
        return []

    async def _run_stage(self, args: dict) -> tuple[dict, bool]:
        """Run a pipeline stage. qualify/analyze/enhance are bounded, so they run
        SYNCHRONOUSLY in the turn and return a real summary. scout is an unbounded crawl,
        so it stays on the worker (reported as started). Result carries ``stage`` so the
        confirmation reply can phrase it."""
        stage = (args.get("stage") or "").lower()
        job_ids = list(args.get("job_ids") or []) or None
        all_qualified = bool(args.get("all_qualified"))
        if stage == "qualify":
            from .qualify_runner import run_qualify_tracked
            return {"stage": "qualify", "status": "done", **(await run_qualify_tracked(self.user_id))}, False
        if stage == "analyze":
            from .analyze_runner import run_analyze_tracked
            s = await run_analyze_tracked(self.user_id, job_ids=job_ids, all_qualified=all_qualified)
            return {"stage": "analyze", "status": "done", "analyzed": s.get("analyzed", 0),
                    "errors": s.get("errors") or {}}, False
        if stage == "enhance":
            from .enhance_runner import run_enhance_tracked
            s = await run_enhance_tracked(self.user_id, job_ids=job_ids, all_qualified=all_qualified)
            return {"stage": "enhance", "status": "done", "suggestions": s.get("suggestions", 0),
                    "errors": s.get("errors") or {}}, False
        if stage == "scout":
            res = await self._call(self.user_id, "pipeline", "trigger_stage", {"stage": "scout"})
            return {"stage": "scout", **(res if isinstance(res, dict) else {})}, False
        return {"error": f"unknown stage {stage!r} (expected scout|qualify|analyze|enhance)"}, False

    async def _cv(self, name: str, args: dict) -> tuple[dict, bool]:
        """Phase 6.5 CV tools. Tailoring/export/re-qualify run SYNCHRONOUSLY here (awaited in
        the chat turn) so the user gets a real 'done' summary, not a 'queued' ticket — the
        scope is bounded (selected jobs / a draft subset), so it completes within the turn.
        approve/reject are scoped db-mcp writes; the report reads recorded scores."""
        if name == "tailor_cvs":
            from .cv_align_runner import run_align
            s = await run_align(self.user_id, list(args.get("job_ids") or []))
            return {"status": "done", "tailored": s.get("tailored", 0),
                    "judged": s.get("judged", False), "aligned": s.get("aligned", 0),
                    "errors": s.get("errors") or {}}, False
        if name == "prepare_cvs":
            from .cv_prepare_runner import run_prepare_cvs
            s = await run_prepare_cvs(self.user_id, list(args.get("job_ids") or []))
            return {"status": "done", "tailored": s.get("tailored", 0), "aligned": s.get("aligned", 0),
                    "exported": s.get("exported", 0), "requalified": s.get("requalified", 0),
                    "errors": s.get("errors") or {}}, False
        if name == "export_cvs":
            from .cv_export_runner import run_export
            s = await run_export(self.user_id, args.get("draft_ids"))
            return {"status": "done", "exported": s.get("exported", 0), "errors": s.get("errors") or {}}, False
        if name == "requalify_cvs":
            from .requalify_runner import run_requalify
            s = await run_requalify(self.user_id, args.get("draft_ids"))
            return {"status": "done", "requalified": s.get("requalified", 0),
                    "improved": s.get("improved", 0), "worse": s.get("worse", 0),
                    "unchanged": s.get("unchanged", 0)}, False
        if name in ("approve_cv_drafts", "reject_cv_drafts"):
            status = "approved" if name == "approve_cv_drafts" else "rejected"
            ids = await self._resolve_cv_ids(args)
            res = await self._call(self.user_id, "db", "set_cv_draft_status",
                                   {"draft_ids": ids, "status": status})
            return res, False
        if name == "cv_improvement_report":
            return await self._cv_report(), True
        return {"error": f"unhandled cv tool {name!r}"}, False

    async def _cv_report(self) -> dict:
        drafts = (await self._call(self.user_id, "db", "get_cv_drafts", {}))["drafts"]
        jobs = (await self._call(self.user_id, "db", "get_qualified_jobs", {"limit": 5000}))["jobs"]
        by_id = {j["id"]: j for j in jobs}
        rows, improved = [], 0
        for d in drafts:
            if d.get("after_score") is None:
                continue
            before, after = d.get("before_score"), d["after_score"]
            delta = round(after - before, 4) if before is not None else None
            improved += bool(delta is not None and delta > 0.0001)
            job = by_id.get(d["job_id"]) or {}
            rows.append({"job_title": job.get("title"), "company": job.get("company"),
                         "before_score": before, "after_score": after, "delta": delta})
        rows.sort(key=lambda r: (r["delta"] if r["delta"] is not None else -1), reverse=True)
        return {"report": rows, "improved": improved, "total": len(rows)}

    # ----------------------------------------------------------------------- #
    # Per-job CV Q&A + editing (role+company resolution; reads + cheap edits)
    # ----------------------------------------------------------------------- #
    async def _all_jobs(self) -> list[dict]:
        res = await self._call(self.user_id, "db", "get_qualified_jobs", {"limit": 5000})
        return res.get("jobs", []) if isinstance(res, dict) else []

    async def _resolve_single_job(self, args: dict) -> tuple[dict | None, dict | None]:
        """Resolve exactly ONE target job from job_ids, the selection, or a free-text
        role+company ``job_ref`` — never asking the user for an id. Returns ``(job, error)``:
        exactly one is non-None."""
        jobs = await self._all_jobs()
        by_id = {j.get("id"): j for j in jobs}
        ids = list(args.get("job_ids") or [])
        if not ids and args.get("use_selected"):
            ids = list(self.selected_job_ids)
        if ids:
            if len(ids) == 1 and ids[0] in by_id:
                return by_id[ids[0]], None
            if len(ids) > 1:
                return None, {"status": "ambiguous",
                              "note": "That points to several jobs — name one role and company."}
            return None, {"status": "not_found", "note": "I couldn't find that job."}
        ref = (args.get("job_ref") or "").strip()
        if ref:
            matches = _match_jobs(jobs, ref)
            if len(matches) == 1:
                return matches[0], None
            if not matches:
                return None, {"status": "no_match",
                              "note": f"No job matches '{ref}'. Try the exact role and company."}
            return None, {"status": "ambiguous",
                          "candidates": [f"{m.get('title')} @ {m.get('company')}" for m in matches[:6]],
                          "note": "Several jobs match — which role and company?"}
        return None, {"status": "need_job",
                      "note": "Which job? Give me the role and company (e.g. 'the Backend role at Stripe')."}

    async def _analysis_for(self, job_id: str) -> dict | None:
        res = await self._call(self.user_id, "db", "get_analyses", {})
        for a in (res.get("analyses") if isinstance(res, dict) else []) or []:
            if a.get("job_id") == job_id:
                ex = a.get("extracted_json") or {}
                return {**ex, "gaps_vs_resume": ex.get("gaps_vs_resume") or (a.get("gaps_json") or {}).get("gaps")}
        return None

    async def _draft_for(self, job_id: str) -> dict | None:
        res = await self._call(self.user_id, "db", "get_cv_drafts", {})
        for d in (res.get("drafts") if isinstance(res, dict) else []) or []:
            if d.get("job_id") == job_id:
                return d
        return None

    async def _base_resume(self) -> dict:
        wrap = (await self._call(self.user_id, "resume", "get_base_resume", {})) or {}
        return (wrap.get("resume") or {}).get("structured") or {}

    async def _cv_query(self, name: str, args: dict) -> tuple[dict, bool]:
        job, err = await self._resolve_single_job(args)
        if err:
            return err, False
        if name == "explain_job":
            analysis = await self._analysis_for(job["id"])
            digest = cv_digest.job_digest(job, analysis)
            if not analysis:
                digest["note"] = "This job isn't analyzed yet — say 'analyze it' for the full breakdown."
            return digest, True
        if name == "show_cv":
            draft = await self._draft_for(job["id"])
            if not draft:
                return {"status": "no_cv",
                        "note": f"No tailored CV is attached to {job.get('title')} @ {job.get('company')} "
                                f"yet — say 'tailor it' or 'prepare a CV' first."}, False
            return cv_digest.cv_sections(draft.get("tailored_content_json") or {}, job), True
        if name in ("edit_cv", "reword_cv"):
            return await self._mutate_cv(name, args, job)
        return {"error": f"unhandled cv-query tool {name!r}"}, False

    async def _mutate_cv(self, name: str, args: dict, job: dict) -> tuple[dict, bool]:
        draft = await self._draft_for(job["id"])
        if not draft:
            return {"status": "no_cv",
                    "note": f"No CV attached to {job.get('title')} @ {job.get('company')} yet — "
                            f"tailor or prepare one first, then I can edit it."}, False
        base = await self._base_resume()
        snapshot = draft.get("locked_snapshot_json") or cv_fields.extract_locked_snapshot(base)
        content = draft.get("tailored_content_json") or {}
        target_label = f"{job.get('title')} @ {job.get('company')}"

        if name == "edit_cv":
            out = cv_edit.edit_tailored_cv(
                content, base, snapshot,
                add_skills=args.get("add_skills"), remove_skills=args.get("remove_skills"),
                add_bullets=args.get("add_bullets"), remove_bullets=args.get("remove_bullets"))
            new_content, changes, warnings = out["content"], out["changes"], out["warnings"]
            if not changes:
                return {"status": "no_change", "job": target_label,
                        "warnings": warnings or ["nothing matched that edit"]}, False
        else:  # reword_cv — one focused model call
            from jobpilot_core import cv_rewriter
            settings = get_settings()
            analysis = await self._analysis_for(job["id"]) or {}
            target = "role" if (args.get("target") or "").lower() == "role" else "summary"
            role_index = 0
            if target == "role":
                exp = content.get("experience") or []
                role_index = cv_edit._match_role(exp, args.get("role_ref"), None) or 0
            res = await cv_rewriter.reword_section(
                base, snapshot, job, analysis, content, target=target,
                instruction=args.get("instruction") or "", role_index=role_index, settings=settings)
            data = res.data or {}
            spliced = dict(content)
            if target == "summary" and data.get("summary"):
                spliced["summary"] = data["summary"]
            elif target == "role" and data.get("bullets"):
                exp = [dict(e) for e in (spliced.get("experience") or [])]
                if role_index < len(exp):
                    exp[role_index]["bullets"] = data["bullets"]
                    spliced["experience"] = exp
            new_content = cv_fields.finalize(spliced, base, snapshot)
            changes = [f"reworded the {'summary' if target == 'summary' else 'bullets'} "
                       f"for {target_label}"]
            warnings = []

        await self._call(self.user_id, "db", "set_cv_draft_content",
                         {"draft_id": draft["id"], "tailored_content_json": new_content})
        return {"status": "edited", "job": target_label, "changes": changes,
                "warnings": warnings,
                "next": "The draft is back to draft_ready — review it, then say "
                        "'approve and export' to re-attach the updated CV."}, False


# --------------------------------------------------------------------------- #
# Turn loop
# --------------------------------------------------------------------------- #
def _last_pending(history: list[dict]) -> dict | None:
    """The pending consequential action proposed by the Concierge on the previous turn
    (recorded in the assistant message metadata), awaiting the user's yes/no."""
    for m in reversed(history):
        if m.get("role") == "assistant":
            pending = (m.get("tool_calls_json") or {}).get("pending_confirmation")
            return pending or None
    return None


def _to_anthropic_messages(history: list[dict]) -> list[dict]:
    """Persisted transcript → Messages API turns (user/assistant text only; tool blocks
    are per-turn and not replayed)."""
    out: list[dict] = []
    for m in history:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _confirm_reply(tool: str, result: dict) -> str:
    """A short, honest summary for the (LLM-free) confirmation turn. Most actions now run
    synchronously, so this reports what ACTUALLY happened, not a queue ticket."""
    if isinstance(result, dict) and result.get("error"):
        return f"That didn't complete — {result['error']}"
    errs = (result.get("errors") or {}) if isinstance(result, dict) else {}
    err_tail = f" ({len(errs)} error(s))" if errs else ""
    if tool == "run_pipeline":
        pid = result.get("pipeline_id")
        if result.get("rejected"):
            return f"A pipeline is already running ({result.get('active_pipeline_id')})."
        return f"Done — the pipeline is queued ({pid})." if pid else "Pipeline queued."
    if tool == "trigger_stage":
        stage = result.get("stage")
        if stage == "qualify":
            return (f"Done — re-qualified the pool: {result.get('qualified', 0)} qualified, "
                    f"{result.get('disqualified', 0)} not (scored {result.get('scored', 0)}, "
                    f"skipped {result.get('skipped', 0)}). Jobs with an exported tailored CV are "
                    f"scored against that CV, otherwise your uploaded résumé.{err_tail}")
        if stage == "analyze":
            return f"Done — analyzed {result.get('analyzed', 0)} job(s).{err_tail} Want tailored CVs or suggestions next?"
        if stage == "enhance":
            return f"Done — generated {result.get('suggestions', 0)} suggestion set(s).{err_tail}"
        if stage == "scout":
            return "Scouting started in the background — new jobs will appear shortly (crawls can take a few minutes)."
        return f"Done — {stage}.{err_tail}"
    if tool == "update_qualifier_config":
        return "Done — updated your qualifier scoring config."
    if tool == "apply_resume":
        if result.get("applied"):
            added = ", ".join(result.get("added_skills") or []) or "no new skills"
            return f"Done — created a new resume version ({result.get('applied_rewrites', 0)} rewrite(s); added: {added})."
        return f"Nothing applied — {result.get('reason', 'no accepted suggestions')}."
    if tool == "tailor_cvs":
        # Only claim a bar was cleared when a Critic actually applied one (cv_max_rounds > 0);
        # in single-pass mode there is no score to report and saying "0 cleared" would lie.
        bar = (f" ({result.get('aligned', 0)} cleared the alignment bar)"
               if result.get("judged") else "")
        return (f"Done — tailored {result.get('tailored', 0)} CV(s){bar}.{err_tail} Review them, "
                f"then say 'approve and export' to attach.")
    if tool == "prepare_cvs":
        return (f"Done — prepared {result.get('tailored', 0)} CV(s), exported {result.get('exported', 0)}, "
                f"re-qualified {result.get('requalified', 0)}.{err_tail} They're attached to the job rows — "
                f"ask for the improvement report to see the lift.")
    if tool == "requalify_cvs":
        return (f"Done — re-qualified {result.get('requalified', 0)} CV(s): "
                f"{result.get('improved', 0)} improved, {result.get('worse', 0)} lower, "
                f"{result.get('unchanged', 0)} unchanged.")
    if tool == "delete_jobs":
        files = result.get("files_deleted") or 0
        extra = f" and {files} attached CV file(s)" if files else ""
        return (f"Done — deleted {result.get('deleted', 0)} job(s){extra} and everything under "
                f"them. Want me to re-run scouting to refill the pool?")
    if tool == "delete_disqualified_jobs":
        files = result.get("files_deleted") or 0
        extra = f" ({files} attached CV file(s) removed)" if files else ""
        return (f"Done — removed {result.get('deleted', 0)} disqualified job(s){extra}. Want me "
                f"to re-qualify what's left, or analyze the qualified set?")
    if tool == "delete_user_data":
        return "Done — all of your data has been erased."
    return f"Done — {tool}."


async def _persist(user_id, chat_session_id, role, content, tool_calls_json=None):
    await mcp_client.db_mcp(user_id, "append_message", {
        "chat_session_id": str(chat_session_id), "role": role,
        "content": content, "tool_calls_json": tool_calls_json})


async def _safe_execute(executor: "ConciergeExecutor", name: str, args: dict, confirmed: bool):
    """Execute a tool, turning a failure into an error result so one bad tool call can't
    crash the whole chat turn."""
    try:
        return await executor.execute(name, args, confirmed=confirmed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("concierge.tool %s failed: %s", name, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}, False


def _selection_preface(selected_job_ids: list[str]) -> str:
    """A trusted, per-turn context line telling the agent how many jobs are selected. Kept
    OUT of the cached system prompt (which is byte-stable) and OUT of the persisted
    transcript, so it never accumulates across turns and never breaks the prompt cache."""
    n = len(selected_job_ids)
    if n == 0:
        return ("[app context — trusted] The user has NO jobs selected right now. If they "
                "refer to 'selected' jobs, ask them to tick jobs in the table first.\n\n")
    return (f"[app context — trusted] The user has {n} job(s) selected in the dashboard. "
            f"To act on the selection, call job tools with use_selected=true — do not echo "
            f"the ids.\n\n")


async def run_turn(
    user_id: uuid.UUID,
    chat_session_id: uuid.UUID,
    user_text: str,
    selected_job_ids: list[str] | None = None,
) -> dict:
    """Run one Concierge turn. Persists the exchange, drives the tool-use loop, and
    returns the reply plus a list of tool-call events (for streaming). ``selected_job_ids``
    is the user's current dashboard selection (trusted request context); job-scoped tools
    resolve "selected" against it. A pending consequential action from the previous turn,
    confirmed by an explicit yes, executes on the fast path (no LLM call) — deterministic
    and free."""
    settings = get_settings()
    selected = list(selected_job_ids or [])
    history = (await mcp_client.db_mcp(
        user_id, "get_messages", {"chat_session_id": str(chat_session_id)}))["messages"]
    executor = ConciergeExecutor(user_id, selected_job_ids=selected)
    await _persist(user_id, chat_session_id, "user", user_text)

    # Fast path: the user is confirming a consequential action proposed last turn. The
    # pending args already carry the selection resolved at propose time, so the confirmed
    # action fires on exactly what was described.
    pending = _last_pending(history)
    if pending and concierge.confirms_action(pending["tool"], user_text):
        result, _ = await _safe_execute(executor, pending["tool"], pending["args"], confirmed=True)
        reply = _confirm_reply(pending["tool"], result if isinstance(result, dict) else {})
        await _persist(user_id, chat_session_id, "assistant", reply)
        logger.info("concierge.turn confirmed=%s", pending["tool"])
        return {"reply": reply, "events": [{"tool": pending["tool"], "status": "confirmed"}],
                "pending": None}

    # Prefix the turn (for the model only) with the current selection; the persisted user
    # message above stays the raw text.
    turn_text = _selection_preface(selected) + user_text
    messages = _to_anthropic_messages(history) + [{"role": "user", "content": turn_text}]
    client = get_client(settings)
    events_out: list[dict] = []
    new_pending: dict | None = None
    resp = None

    try:
        for _ in range(settings.concierge_max_tool_iterations):
            resp = await client.messages.create(
                model=settings.concierge_model,
                max_tokens=settings.concierge_max_tokens,
                system=concierge.system_blocks(),
                tools=concierge.TOOLS,
                messages=messages,
                thinking={"type": "disabled"},  # interactive chat: low latency, no scratchpad
            )
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                break
            tool_results = []
            for tu in tool_uses:
                # Never confirmed inside the loop — a consequential call returns
                # confirmation_required and is held for the user's next explicit yes.
                result, is_data = await _safe_execute(executor, tu.name, tu.input or {}, confirmed=False)
                events_out.append({"tool": tu.name,
                                   "status": result.get("status", "ok") if isinstance(result, dict) else "ok"})
                if isinstance(result, dict) and result.get("status") == "confirmation_required":
                    # Store the selection-RESOLVED args so the confirmed run targets exactly
                    # what the confirm prompt described (not the raw use_selected marker).
                    new_pending = {"tool": tu.name, "args": result.get("resolved_args") or (tu.input or {})}
                    # resolved_args is bookkeeping (may hold job UUIDs) — keep it out of the
                    # model's view; the prompt text alone is what it should relay.
                    result = {k: v for k, v in result.items() if k != "resolved_args"}
                content = concierge.wrap_retrieved(tu.name, json.dumps(result)) if is_data else json.dumps(result)
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
            messages.append({"role": "user", "content": tool_results})
    finally:
        try:
            await client.close()  # release the httpx pool with the loop that owns it
        except Exception:  # noqa: BLE001
            pass

    reply = "".join(b.text for b in (resp.content if resp else []) if getattr(b, "type", None) == "text")
    await _persist(user_id, chat_session_id, "assistant", reply,
                   {"pending_confirmation": new_pending} if new_pending else None)
    logger.info("concierge.turn tools=%d pending=%s", len(events_out), bool(new_pending))
    return {"reply": reply, "events": events_out, "pending": new_pending}
