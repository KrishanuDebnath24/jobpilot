"""Scout orchestration — the fan-out + shared tail.

For a user: read their search config (via config-mcp), run each enabled connector
(via sources-mcp), normalize + dedupe every raw job (deterministically, in
``jobpilot_core.scout``), and persist through **db-mcp** ``upsert_jobs`` — never a
direct ORM write. One connector failing is recorded, not fatal. The whole path is
idempotent: re-running produces no duplicate rows (dedupe by ``dedupe_hash``).
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import events, tracing
from jobpilot_core.ats_boards import extract_greenhouse_slugs, merge_boards
from jobpilot_core.profile_search import derive_search_terms, merge_search_terms
from jobpilot_core.scout import normalize_raw, unique_contribution
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.scout")


async def _effective_terms(user_id: uuid.UUID, config: dict) -> tuple[dict, dict | None]:
    """Merge the user's resume-derived search terms into the configured ones (Step 1).

    Returns ``(effective_terms, derived_or_none)``. Never fatal: a missing resume or a
    resume-mcp hiccup falls back to the configured terms unchanged."""
    settings = get_settings()
    base = {
        "roles": config.get("roles") or [],
        "keywords": config.get("keywords") or [],
        "locations": config.get("locations") or [],
    }
    if not settings.scout_profile_search:
        return base, None
    try:
        wrap = (await mcp_client.resume_mcp(user_id, "get_structured_resume", {})).get("resume")
        resume = wrap.get("structured") if wrap else None
    except Exception as exc:  # noqa: BLE001 - profile enrichment must never fail scout
        logger.warning("scout.profile: resume fetch failed, using configured terms: %s", exc)
        resume = None
    if not resume:
        return base, None
    derived = derive_search_terms(
        resume,
        max_roles=settings.scout_profile_max_roles,
        max_keywords=settings.scout_profile_max_keywords,
    )
    merged = merge_search_terms(
        config, derived,
        max_roles=settings.scout_profile_max_roles,
        max_keywords=settings.scout_profile_max_keywords,
    )
    logger.info("scout.profile derived roles=%s keywords=%s", derived["roles"], derived["keywords"])
    return merged, derived


async def _emit(user_id: uuid.UUID, event: dict) -> None:
    """Publish a pipeline event (fire-and-forget; a failed publish never fails the
    run — the persisted pipeline_runs row is the source of truth)."""
    try:
        await events.publish_event(get_settings().redis_url, user_id, event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scout.emit failed: %s", exc)

# High enough to cover a full scout batch for the counts below (Phase-2 volumes).
_COUNT_LIMIT = 5000


async def _count_jobs(user_id: uuid.UUID) -> int:
    res = await mcp_client.db_mcp(user_id, "get_jobs", {"limit": _COUNT_LIMIT})
    return len(res["jobs"])


async def _run_connector(user_id, name, tool, args, raw_jobs, by_connector, errors, usage):
    """Call one sources-mcp connector; append its raw jobs. Failure is recorded,
    never fatal (a broken source must not kill the run)."""
    try:
        res = await mcp_client.sources_mcp(user_id, tool, args)
        jobs = res.get("jobs", [])
        raw_jobs.extend(jobs)
        by_connector[name] = len(jobs)
        if isinstance(res.get("usage"), dict):  # crawler reports LLM token usage
            usage["input_tokens"] += res["usage"].get("input_tokens", 0)
            usage["output_tokens"] += res["usage"].get("output_tokens", 0)
            usage["estimated_cost_usd"] += res["usage"].get("estimated_cost_usd", 0.0)
        logger.info("scout.connector name=%s fetched=%d", name, len(jobs))
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, capture the reason
        errors[name] = f"{type(exc).__name__}: {exc}"
        by_connector[name] = 0
        logger.warning("scout.connector name=%s FAILED: %s", name, exc)


async def run_scout(user_id: uuid.UUID, config: dict | None = None) -> dict:
    """Fan out enabled connectors, normalize+dedupe, persist via db-mcp. Returns
    counts (fetched / unique / new / deduped) + per-connector tallies and errors."""
    if config is None:
        config = (await mcp_client.config_mcp(user_id, "get_search_config", {}))["config"]

    settings = get_settings()
    sources = config.get("sources") or []
    target_employers = config.get("target_employers") or []

    # Step 1: search terms = configured terms unioned with resume-derived ones.
    terms, derived = await _effective_terms(user_id, config)
    roles, keywords, locations = terms["roles"], terms["keywords"], terms["locations"]

    raw_jobs: list[dict] = []
    by_connector: dict[str, int] = {}
    errors: dict[str, str] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}

    if "adzuna" in sources:
        await _run_connector(
            user_id, "adzuna", "search_adzuna",
            {"roles": roles, "locations": locations, "keywords": keywords},
            raw_jobs, by_connector, errors, usage,
        )

    # Aggregators give Republic-of-Ireland coverage (Adzuna can't). Free ones are on
    # by default; JSearch is metered and only runs when explicitly in `sources`. Run
    # them (and the crawler) BEFORE Greenhouse so we can harvest ATS slugs from their
    # apply links and fold those boards into the Greenhouse fan-out below (Step 2).
    #
    # Free-text engines index by role and collapse on a long AND query, so we query
    # them with the SINGLE primary role only — appending the skill list (or even a
    # second role) empties the result set. Verified live: one role ~50 jobs vs 0 with
    # the full skill list. Skills still enrich Adzuna, Greenhouse, and the qualifier.
    roi_location = (locations or ["Dublin, Ireland"])[0]
    primary_role = roles[:1] or keywords[:1]
    for name in ("jooble", "careerjet", "jsearch"):
        if name in sources:
            await _run_connector(
                user_id, name, f"search_{name}",
                {"roles": primary_role, "keywords": [], "location": roi_location},
                raw_jobs, by_connector, errors, usage,
            )

    if "crawl" in sources:
        await _run_connector(
            user_id, "crawl", "run_crawl",
            {"roles": roles, "keywords": keywords, "location": (locations or ["dublin"])[0]},
            raw_jobs, by_connector, errors, usage,
        )

    # Step 2: Greenhouse over configured employers + curated Ireland defaults + slugs
    # harvested from the apply links of everything fetched so far. Per-board 404s are
    # recorded, never fatal; the whole list is capped for politeness.
    greenhouse_boards: list[str] = []
    if "greenhouse" in sources:
        harvested = extract_greenhouse_slugs(rj.get("url") for rj in raw_jobs)
        greenhouse_boards = merge_boards(
            target_employers, harvested, cap=settings.scout_max_greenhouse_boards
        )
        if harvested:
            logger.info("scout.greenhouse harvested slugs=%s", sorted(harvested))
        for company in greenhouse_boards:
            await _run_connector(
                user_id, f"greenhouse:{company}", "fetch_greenhouse",
                {"company": company, "roles": roles, "locations": locations, "keywords": keywords},
                raw_jobs, by_connector, errors, usage,
            )

    # --- shared tail: deterministic normalize + dedupe, then persist via db-mcp ---
    before = await _count_jobs(user_id)
    normalized = [normalize_raw(rj).model_dump() for rj in raw_jobs]
    unique_hashes = {n["dedupe_hash"] for n in normalized}
    # Addendum 2.2 (revised) step 5: per-connector unique-after-dedup contribution —
    # jobs that survive dedup ONLY via that source. The measured keep/drop signal (a
    # source is judged on the unique breadth it adds, never on the "snippet" label).
    unique_by_connector = unique_contribution(raw_jobs, [n["dedupe_hash"] for n in normalized])
    if normalized:
        await mcp_client.db_mcp(user_id, "upsert_jobs", {"jobs": normalized})
    after = await _count_jobs(user_id)

    summary = {
        "fetched": len(raw_jobs),
        "unique": len(unique_hashes),
        "new": after - before,
        "deduped": len(raw_jobs) - len(unique_hashes),
        "by_connector": by_connector,
        "unique_by_connector": unique_by_connector,
        "errors": errors,
        "llm_usage": {**usage, "estimated_cost_usd": round(usage["estimated_cost_usd"], 6)},
        # Transparency: what the profile derived and how many boards we polled.
        "profile_terms": derived,
        "greenhouse_boards": len(greenhouse_boards),
    }
    logger.info("scout.summary %s", summary)
    return summary


@tracing.traced_stage("scout")
async def run_scout_tracked(
    user_id: uuid.UUID, config: dict | None = None, pipeline_id: uuid.UUID | str | None = None
) -> dict:
    """Run Scout, recording a ``pipeline_runs`` row (via db-mcp) with the run's
    counts, correlated under ``pipeline_id``. Emits ``scout.started`` / ``.completed``
    / ``.failed`` on the event bus. A connector failing is captured in the summary;
    only a catastrophic failure (no data at all) marks the whole run 'failed'."""
    pid = str(pipeline_id) if pipeline_id else str(uuid.uuid4())
    run = await mcp_client.db_mcp(
        user_id, "create_pipeline_run", {"stage": "scout", "pipeline_id": pid}
    )
    run_id = run["id"]
    await _emit(user_id, events.make_event("scout", events.STARTED, pipeline_id=pid))
    try:
        summary = await run_scout(user_id, config)
    except Exception as exc:  # noqa: BLE001 - record the failure, then re-raise
        await mcp_client.db_mcp(user_id, "finish_pipeline_run", {
            "run_id": run_id, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        await _emit(user_id, events.make_event(
            "scout", events.FAILED, pipeline_id=pid, error=f"{type(exc).__name__}: {exc}"))
        raise
    errors = summary.get("errors") or {}
    err_str = f"{len(errors)} connector error(s): {', '.join(errors)}" if errors else None
    await mcp_client.db_mcp(user_id, "finish_pipeline_run", {
        "run_id": run_id, "status": "completed", "counts": summary, "error": err_str})
    summary["run_id"] = run_id
    summary["pipeline_id"] = pid
    # Embed-on-write: newly-scouted jobs get embedded asynchronously (never blocks scout).
    from .embed_runner import enqueue_embed
    enqueue_embed(user_id, ["jobs"])
    await _emit(user_id, events.make_event(
        "scout", events.COMPLETED, pipeline_id=pid, counts=summary))
    return summary
