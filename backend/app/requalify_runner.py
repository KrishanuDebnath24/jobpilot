"""Mass re-qualification of tailored CVs (Phase 6.5, 6.5f) — runs in the Celery worker.

Re-runs the Phase-3 Qualifier scoring each **tailored** CV draft against its target job
and records ``after_score`` next to the ``before_score`` already on record. The
deterministic pass runs locally (free); the Haiku fit-refine for gate-passers goes
through the **Batch API** (Phase 5 path) when the selection is large enough, else sync.

The score lift is legitimate by construction: the tailored content passed through the
locked-field enforcement + non-fabrication guards, so a higher score reflects better
presentation of TRUE experience, not gaming. Returns an improvement report (per-job
deltas) for the Concierge to relay.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from jobpilot_core import qualifier
from jobpilot_core.llm import collect_batch_results, get_batch_status, submit_batch
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.requalify")


async def _context(user_id: uuid.UUID):
    qcfg = (await mcp_client.config_mcp(user_id, "get_qualifier_config", {}))["config"]
    scfg = (await mcp_client.config_mcp(user_id, "get_search_config", {}))["config"]
    jobs = (await mcp_client.db_mcp(user_id, "get_qualified_jobs", {"limit": 5000}))["jobs"]
    return qcfg, (scfg.get("locations") or []), {j["id"]: j for j in jobs}


async def run_requalify(user_id: uuid.UUID, draft_ids: list[str] | None = None) -> dict:
    """Re-qualify tailored drafts (a subset, or all approved) and report deltas."""
    settings = get_settings()
    qcfg, prefs, jobs = await _context(user_id)

    drafts = (await mcp_client.db_mcp(user_id, "get_cv_drafts", {}))["drafts"]
    if draft_ids:
        wanted = set(draft_ids)
        drafts = [d for d in drafts if d["id"] in wanted]
    else:
        drafts = [d for d in drafts if d["status"] == "approved"]

    # 1) deterministic pass for every draft (free); collect gate-passers for the LLM refine
    dets: dict[str, dict] = {}
    to_refine: dict[str, dict] = {}  # draft_id -> {resume, job}
    errors: dict[str, str] = {}
    for d in drafts:
        did = d["id"]
        job = jobs.get(d["job_id"])
        cv = d.get("tailored_content_json")
        if job is None or not cv:
            errors[did] = "missing job or tailored content"
            continue
        det = qualifier.score_deterministic(cv, job, qcfg, preferred_locations=prefs)
        dets[did] = det
        if det["must_have_gate"] == "pass":
            to_refine[did] = {"resume": cv, "job": job}

    # 2) refine gate-passers — Batch API when large, else sync
    refined: dict[str, dict] = {}
    mode = "batch" if len(to_refine) >= settings.analyze_batch_threshold else "sync"
    if to_refine and mode == "batch":
        requests = [
            {"custom_id": did, "params": qualifier.refine_batch_params(v["resume"], v["job"], settings)}
            for did, v in to_refine.items()
        ]
        bid = await submit_batch(settings, requests)
        deadline = time.monotonic() + settings.batch_max_wait_seconds
        while time.monotonic() < deadline:
            if await get_batch_status(settings, bid) == "ended":
                break
            await asyncio.sleep(settings.batch_poll_interval_seconds)
        results, _ = await collect_batch_results(
            settings, bid, qualifier.REFINE_TOOL, settings.scout_extractor_model)
        refined = results
    elif to_refine:
        for did, v in to_refine.items():
            try:
                refined[did] = (await qualifier.refine_with_llm(v["resume"], v["job"], qcfg, settings)).data
            except Exception as exc:  # noqa: BLE001
                errors[did] = f"refine: {type(exc).__name__}: {exc}"

    # 3) combine + persist after_score; build the delta report
    report: list[dict] = []
    improved = unchanged = worse = 0
    for d in drafts:
        did = d["id"]
        if did not in dets:
            continue
        final = qualifier.combine(dets[did], refined.get(did), qcfg)
        after = final["score"]
        await mcp_client.db_mcp(user_id, "set_cv_draft_after_score",
                                {"draft_id": did, "after_score": after})
        before = d.get("before_score")
        job = jobs.get(d["job_id"]) or {}
        delta = round(after - before, 4) if before is not None else None
        if delta is not None:
            improved += delta > 0.0001
            worse += delta < -0.0001
            unchanged += abs(delta) <= 0.0001
        report.append({
            "draft_id": did, "job_id": d["job_id"],
            "job_title": job.get("title"), "company": job.get("company"),
            "before_score": before, "after_score": after, "delta": delta,
            "improved": bool(delta is not None and delta > 0.0001),
        })

    report.sort(key=lambda r: (r["delta"] if r["delta"] is not None else -1), reverse=True)
    summary = {
        "requalified": len(report), "mode": mode,
        "improved": improved, "unchanged": unchanged, "worse": worse,
        "report": report, "errors": errors,
    }
    logger.info("requalify.summary requalified=%d improved=%d mode=%s",
                len(report), improved, mode)
    return summary
