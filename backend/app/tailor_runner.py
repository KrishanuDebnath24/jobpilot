"""Tailor + Critic orchestration (Phase 6.5, 6.5b/6.5c) — runs in the Celery worker.

For each user-selected job: pull the base resume + that job's Phase 5 analysis, generate
a tailored draft (Tailor, Sonnet), then run the bounded tailor→critic→refine loop. A
**code validator** (``cv_fields``) runs alongside the Critic every round and, at the end,
``enforce_locked`` + ``drop_fabricated_skills`` make the persisted draft honest **by
construction** — locked fields byte-identical to the snapshot, no invented skills — no
matter what the model emitted. Reads via MCP; writes drafts via db-mcp. CV content is
never logged.
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import cv_fields
from jobpilot_core.critic import critique_cv
from jobpilot_core.llm import LLMUsage
from jobpilot_core.settings import get_settings
from jobpilot_core.tailor import tailor_cv

from . import mcp_client

logger = logging.getLogger("jobpilot.tailor")


async def _base_resume(user_id: uuid.UUID) -> tuple[dict, str | None]:
    wrap = (await mcp_client.resume_mcp(user_id, "get_structured_resume", {}))["resume"]
    if not wrap or not wrap.get("structured"):
        raise RuntimeError("no resume on file — upload a resume before tailoring")
    return wrap["structured"], wrap.get("id")


async def _analyses_by_job(user_id: uuid.UUID) -> dict[str, dict]:
    analyses = (await mcp_client.db_mcp(user_id, "get_analyses", {}))["analyses"]
    out: dict[str, dict] = {}
    for a in analyses:
        jid = a.get("job_id")
        ex = a.get("extracted_json") or {}
        if jid and jid not in out:  # newest-first from db-mcp; keep the latest
            out[jid] = {**ex, "gaps_vs_resume": ex.get("gaps_vs_resume") or (a.get("gaps_json") or {}).get("gaps")}
    return out


async def _jobs_by_id(user_id: uuid.UUID) -> dict[str, dict]:
    jobs = (await mcp_client.db_mcp(user_id, "get_qualified_jobs", {"limit": 5000}))["jobs"]
    return {j["id"]: j for j in jobs}


async def _tailor_one(user_id, base, snapshot, job, analysis, settings, usage) -> dict:
    """Tailor + bounded critic loop for one job. Returns {tailored_content, critic_notes}.
    The final draft is honest by construction (enforce_locked + drop_fabricated_skills)."""
    res = await tailor_cv(base, snapshot, job, analysis, settings)
    usage.add(res.usage.input_tokens, res.usage.output_tokens,
              cache_read=res.usage.cache_read_input_tokens,
              cache_creation=res.usage.cache_creation_input_tokens)
    current = res.data or {}
    rounds: list[dict] = []

    for _ in range(settings.critic_max_rounds):
        v = cv_fields.validate_tailored(current, base, snapshot)
        cres = await critique_cv(base, snapshot, job, analysis, current, settings,
                                 code_violations=v["violations"])
        usage.add(cres.usage.input_tokens, cres.usage.output_tokens,
                  cache_read=cres.usage.cache_read_input_tokens,
                  cache_creation=cres.usage.cache_creation_input_tokens)
        c = cres.data or {}
        rounds.append({"verdict": c.get("verdict"), "issues": c.get("issues") or []})
        current = c.get("refined_cv") or current
        if c.get("verdict") == "pass" and cv_fields.validate_tailored(current, base, snapshot)["ok"]:
            break

    # Hard guarantee (defense in depth beyond the prompt + critic): locked fields become
    # byte-identical to the snapshot; any un-grounded skill is dropped.
    pre = cv_fields.validate_tailored(current, base, snapshot)
    final = cv_fields.drop_fabricated_skills(cv_fields.enforce_locked(current, snapshot), base)
    post = cv_fields.validate_tailored(final, base, snapshot)
    notes = {
        "rounds": rounds,
        "caught_before_enforce": {"violations": pre["violations"], "fabricated": pre["fabricated"]},
        "final_ok": post["ok"],
        "change_notes": final.get("change_notes") or [],
    }
    return {"tailored_content": final, "critic_notes": notes}


async def run_tailor(user_id: uuid.UUID, job_ids: list[str]) -> dict:
    """Tailor a CV for each selected job (analysis-driven, locked-safe). Persists a
    ``draft_ready`` cv_draft per job. Per-job error isolation. Bounded selection."""
    settings = get_settings()
    job_ids = list(dict.fromkeys(job_ids))[: settings.cv_tailor_max_jobs]
    base, base_version = await _base_resume(user_id)
    snapshot = cv_fields.extract_locked_snapshot(base)
    analyses = await _analyses_by_job(user_id)
    jobs = await _jobs_by_id(user_id)

    usage = LLMUsage(model=settings.tailor_model)
    tailored, errors = [], {}
    for jid in job_ids:
        job = jobs.get(jid)
        if job is None:
            errors[jid] = "job not found"
            continue
        analysis = analyses.get(jid)
        if not analysis:
            errors[jid] = "no analysis on file — analyze this job first (Phase 5)"
            continue
        try:
            out = await _tailor_one(user_id, base, snapshot, job, analysis, settings, usage)
            before = (job.get("qualification") or {}).get("score")
            saved = await mcp_client.db_mcp(user_id, "create_cv_draft", {
                "job_id": jid,
                "tailored_content_json": out["tailored_content"],
                "locked_snapshot_json": snapshot,
                "base_resume_version": base_version,
                "critic_notes_json": out["critic_notes"],
                "before_score": before,
            })
            tailored.append({"job_id": jid, "draft_id": saved["draft"]["id"]})
        except Exception as exc:  # noqa: BLE001 - one bad job must not sink the batch
            errors[jid] = f"{type(exc).__name__}: {exc}"
            logger.warning("tailor.job id=%s FAILED: %s", jid, exc)

    summary = {"tailored": len(tailored), "drafts": tailored, "errors": errors,
               "llm_usage": usage.as_dict()}
    logger.info("tailor.summary tailored=%d errors=%d cost=%.6f",
                len(tailored), len(errors), usage.cost_usd)
    return summary
