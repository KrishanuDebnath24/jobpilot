"""Enhancer orchestration (Phase 5, 5d/5e).

Reads persisted ``analyses`` + resume via MCP and produces grounded, non-fabricating
``suggestions``. Selected ``job_ids`` → one suggestion per job (from that job's
analysis). All-qualified → the deterministic aggregate → one frequency-prioritized
aggregate suggestion (``job_id`` NULL). The Enhancer runs on the Sonnet tier; if that
model isn't accessible it falls back to the Analyzer (Haiku) so a run still completes.
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import analyzer, enhancer, events
from jobpilot_core.llm import LLMUsage
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.enhance")


async def _emit(user_id: uuid.UUID, event: dict) -> None:
    try:
        await events.publish_event(get_settings().redis_url, user_id, event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("enhance.emit failed: %s", exc)


async def _call_enhancer(coro_factory, settings):
    """Run an enhancer call, falling back from the Sonnet tier to the Analyzer model
    (Haiku) if the configured enhancer model is unavailable (access/400)."""
    try:
        return await coro_factory(settings.enhancer_model)
    except Exception as exc:  # noqa: BLE001
        if settings.enhancer_model == settings.analyzer_model:
            raise
        logger.warning("enhance.model %s failed (%s) — falling back to %s",
                       settings.enhancer_model, type(exc).__name__, settings.analyzer_model)
        return await coro_factory(settings.analyzer_model)


def _split(out: dict, extra: dict) -> dict:
    """Map enhancer output to the suggestions columns."""
    skills = out.get("skills_to_add") or []
    return {
        "skills_to_add": [s.get("skill") for s in skills if s.get("skill")],
        "projects_json": {"projects": out.get("projects") or [], "skills_detail": skills, **extra},
        "bullet_rewrites_json": {"bullet_rewrites": out.get("bullet_rewrites") or []},
    }


async def run_enhance(
    user_id: uuid.UUID,
    job_ids: list[str] | None = None,
    all_qualified: bool = False,
    pipeline_id: uuid.UUID | str | None = None,
) -> dict:
    settings = get_settings()
    pid = str(pipeline_id) if pipeline_id else None
    batch_id = str(uuid.uuid4())

    resume_wrap = (await mcp_client.resume_mcp(user_id, "get_structured_resume", {}))["resume"]
    if not resume_wrap or not resume_wrap.get("structured"):
        raise RuntimeError("no resume on file — upload a resume before enhancing")
    resume = resume_wrap["structured"]

    analyses = (await mcp_client.db_mcp(user_id, "get_analyses", {}))["analyses"]
    by_job = {a["job_id"]: a for a in analyses if a.get("job_id")}

    usage = LLMUsage(model=settings.enhancer_model)
    errors: dict[str, str] = {}
    await _emit(user_id, events.make_event("enhance", events.STARTED, pipeline_id=pid))

    if all_qualified:
        extracted = [a.get("extracted_json") or {} for a in analyses]
        if not extracted:
            raise RuntimeError("no analyses to aggregate — run Analyze first")
        aggregate = analyzer.aggregate(extracted)
        res = await _call_enhancer(
            lambda m: enhancer.enhance_aggregate(aggregate, resume, settings, model=m), settings)
        usage.add(res.usage.input_tokens, res.usage.output_tokens,
                  cache_read=res.usage.cache_read_input_tokens,
                  cache_creation=res.usage.cache_creation_input_tokens)
        cols = _split(res.data or {}, {"mode": "batch", "aggregate": aggregate})
        await mcp_client.db_mcp(user_id, "upsert_suggestion", {"job_id": None, "batch_id": batch_id, **cols})
        produced = 1
    else:
        produced = 0
        targets = [j for j in (job_ids or []) if j in by_job]
        for job_id in targets:
            try:
                res = await _call_enhancer(
                    lambda m, a=by_job[job_id]: enhancer.enhance_single(
                        a.get("extracted_json") or {}, resume, settings, model=m), settings)
                usage.add(res.usage.input_tokens, res.usage.output_tokens,
                          cache_read=res.usage.cache_read_input_tokens,
                          cache_creation=res.usage.cache_creation_input_tokens)
                cols = _split(res.data or {}, {"mode": "single"})
                await mcp_client.db_mcp(user_id, "upsert_suggestion",
                                        {"job_id": job_id, "batch_id": batch_id, **cols})
                produced += 1
            except Exception as exc:  # noqa: BLE001
                errors[job_id] = f"{type(exc).__name__}: {exc}"
                logger.warning("enhance.job id=%s FAILED: %s", job_id, exc)
        for job_id in (job_ids or []):
            if job_id not in by_job:
                errors[job_id] = "no analysis (analyze first)"

    summary = {
        "suggestions": produced,
        "mode": "batch" if all_qualified else "single",
        "batch_id": batch_id,
        "errors": errors,
        "llm_usage": usage.as_dict(),
    }
    logger.info("enhance.summary produced=%d mode=%s cost=%.6f",
                produced, summary["mode"], usage.cost_usd)
    await _emit(user_id, events.make_event("enhance", events.COMPLETED, pipeline_id=pid, counts=summary))
    return summary


async def run_enhance_tracked(
    user_id: uuid.UUID,
    job_ids: list[str] | None = None,
    all_qualified: bool = False,
    pipeline_id: uuid.UUID | str | None = None,
) -> dict:
    pid = str(pipeline_id) if pipeline_id else str(uuid.uuid4())
    run = await mcp_client.db_mcp(user_id, "create_pipeline_run", {"stage": "enhance", "pipeline_id": pid})
    run_id = run["id"]
    try:
        summary = await run_enhance(user_id, job_ids=job_ids, all_qualified=all_qualified, pipeline_id=pid)
    except Exception as exc:  # noqa: BLE001
        await mcp_client.db_mcp(user_id, "finish_pipeline_run", {
            "run_id": run_id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        await _emit(user_id, events.make_event(
            "enhance", events.FAILED, pipeline_id=pid, error=f"{type(exc).__name__}: {exc}"))
        raise
    errors = summary.get("errors") or {}
    err_str = f"{len(errors)} job error(s)" if errors else None
    await mcp_client.db_mcp(user_id, "finish_pipeline_run", {
        "run_id": run_id, "status": "completed", "counts": summary, "error": err_str})
    summary["run_id"] = run_id
    summary["pipeline_id"] = pid
    return summary
