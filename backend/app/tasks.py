"""Celery tasks. Stage fan-outs (Scout/Qualify) run on the default data-plane
queue; the LangGraph orchestrator (``pipeline.run_full``) runs on its own
``orchestrator`` queue so awaiting a stage never blocks a stage from running."""

from __future__ import annotations

import uuid

from .analyze_runner import run_analyze_tracked
from .asyncrun import run_async
from .celery_app import celery
from .embed_runner import run_embed
from .enhance_runner import run_enhance_tracked
from .orchestrator import run_pipeline_graph
from .qualify_runner import run_qualify_tracked
from .resolve_runner import resolve_apply_targets
from .scout_runner import run_scout_tracked


@celery.task(name="scout.run")
def scout_run(user_id: str, pipeline_id: str | None = None) -> dict:
    """Background Scout: fan out connectors -> normalize/dedupe -> persist via
    db-mcp, recording a pipeline_runs row correlated under pipeline_id. Returns the
    run summary."""
    return run_async(run_scout_tracked(uuid.UUID(user_id), pipeline_id=pipeline_id))


@celery.task(name="qualify.run")
def qualify_run(user_id: str, requalify_all: bool = False, pipeline_id: str | None = None) -> dict:
    """Background Qualify: hybrid-score the job pool (deterministic gate + gated
    Haiku refine), persist qualifications via db-mcp, record a pipeline_runs row."""
    return run_async(
        run_qualify_tracked(uuid.UUID(user_id), requalify_all=requalify_all, pipeline_id=pipeline_id)
    )


@celery.task(name="analyze.run")
def analyze_run(
    user_id: str,
    job_ids: list[str] | None = None,
    all_qualified: bool = False,
    pipeline_id: str | None = None,
) -> dict:
    """Background Analyze (Phase 5): extract structured analyses over a scope (selected
    jobs or all qualified) — cached sync calls or the Batch API — then a deterministic
    aggregate. Persists via db-mcp, records a pipeline_runs row."""
    return run_async(run_analyze_tracked(
        uuid.UUID(user_id), job_ids=job_ids, all_qualified=all_qualified, pipeline_id=pipeline_id))


@celery.task(name="enhance.run")
def enhance_run(
    user_id: str,
    job_ids: list[str] | None = None,
    all_qualified: bool = False,
    pipeline_id: str | None = None,
) -> dict:
    """Background Enhance (Phase 5): grounded, non-fabricating suggestions from
    analyses (single) or the aggregate (batch). Persists via db-mcp + tracks the run."""
    return run_async(run_enhance_tracked(
        uuid.UUID(user_id), job_ids=job_ids, all_qualified=all_qualified, pipeline_id=pipeline_id))


# --------------------------------------------------------------------------- #
# Orchestrator — the durable control-plane task (LangGraph). acks_late +
# reject_on_worker_lost so a task killed mid-pipeline is redelivered and RESUMES
# from its Postgres checkpoint (rather than being lost).
# --------------------------------------------------------------------------- #
@celery.task(name="pipeline.run_full", acks_late=True, reject_on_worker_lost=True)
def run_full_pipeline(user_id: str, pipeline_id: str) -> dict:
    """Run the autonomous Scout→Qualify graph for one execution. Returns immediately
    to the caller (it's enqueued); executes here in the orchestrator worker."""
    return run_async(run_pipeline_graph(user_id, pipeline_id))


@celery.task(name="cv.tailor")
def cv_tailor(user_id: str, job_ids: list[str]) -> dict:
    """Phase 6.5: run the Rewriter⇄Critic alignment loop per selected job (locked-safe,
    evidence-gated) and persist draft_ready cv_drafts. Sonnet — data-plane worker."""
    from .cv_align_runner import run_align

    return run_async(run_align(uuid.UUID(user_id), job_ids))


@celery.task(name="cv.export")
def cv_export(user_id: str, draft_ids: list[str] | None = None) -> dict:
    """Phase 6.5: Tailor approved drafts into A4 Irish-format ATS-safe PDF+DOCX (no LLM)."""
    from .cv_export_runner import run_export

    return run_async(run_export(uuid.UUID(user_id), draft_ids))


@celery.task(name="cv.prepare")
def cv_prepare(user_id: str, job_ids: list[str]) -> dict:
    """One-click per-job CV: Analyze → Align → approve → Tailor/Export for the selected
    jobs, so a downloadable tailored CV attaches to each job row."""
    from .cv_prepare_runner import run_prepare_cvs

    return run_async(run_prepare_cvs(uuid.UUID(user_id), job_ids))


@celery.task(name="cv.gap_close")
def cv_gap_close(user_id: str, job_ids: list[str], refine: bool = False) -> dict:
    """Phase 6.6: propose gap-closing CV content per selected job (unconstrained by résumé
    evidence). Proposals only — nothing reaches a CV until the user accepts each item.

    ``refine`` runs propose -> critique -> propose: a second agent reviews the set for
    defensibility and writing quality, and the proposer answers the findings. Costs up to
    two extra calls per job, so it is a separate user action rather than the default."""
    from .gap_close_runner import run_gap_close

    return run_async(run_gap_close(uuid.UUID(user_id), job_ids, refine=refine))


@celery.task(name="cv.requalify")
def cv_requalify(user_id: str, draft_ids: list[str] | None = None) -> dict:
    """Phase 6.5: re-qualify tailored drafts vs their jobs (batched) + delta report."""
    from .requalify_runner import run_requalify

    return run_async(run_requalify(uuid.UUID(user_id), draft_ids))


@celery.task(name="embed.run")
def embed_run(user_id: str, kinds: list[str] | None = None) -> dict:
    """Backfill / embed-on-write (Phase 7): embed the user's un-embedded jobs/analyses/
    suggestions/resume into the pgvector columns. Enqueued after a write (scout/analyze/
    enhance/resume-upload) so embedding never blocks the write, and by the backfill
    endpoint for existing rows. Idempotent."""
    return run_async(run_embed(uuid.UUID(user_id), kinds=kinds))


@celery.task(name="pipeline.scheduler_tick")
def scheduler_tick() -> dict:
    """Beat-driven: fire a full pipeline for every user whose schedule is due,
    respecting the one-active-pipeline guard. Runs on the data-plane worker."""
    from .scheduler import run_scheduler_tick

    return run_async(run_scheduler_tick())


@celery.task(name="apply.resolve")
def apply_resolve(user_id: str, limit: int = 200, force: bool = False) -> dict:
    """Apply agent stage 2: follow each listing's redirect chain to the page that hosts
    the application form, and record which ATS serves it. Link-following only — no
    browser, no LLM, nothing submitted."""
    return run_async(resolve_apply_targets(uuid.UUID(user_id), limit=limit, force=force))
