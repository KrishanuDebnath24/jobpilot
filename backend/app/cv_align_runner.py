"""CV alignment orchestration — runs the Rewriter⇄Critic loop in the Celery worker.

For each user-**selected** job: make sure that job has a Phase-5 analysis (analysing the
ones that don't, so this entry point is self-sufficient), pull the base resume + locked
snapshot, then run :func:`jobpilot_core.cv_loop.align_cv` until the Critic calls the CV
aligned or the round cap is reached. The loop's output is already honest by construction
(``cv_fields.finalize`` runs inside every round), and the per-round audit trail — scores,
issues, integrity flags, and which skills were added or code-rejected — is persisted on
the draft so a reviewer can see exactly how the CV got there.

Reads via MCP; writes drafts via db-mcp. CV content is never logged.
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import cv_fields, gap_closer
from jobpilot_core.cv_loop import align_cv
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.cv_align")


async def _base_resume(user_id: uuid.UUID) -> tuple[dict, str | None]:
    # The WORKING resume = the latest uploaded file (not an assembled apply-to-resume
    # version), so an aligned CV is tagged with the same parent id the qualifier scores
    # against — keeping the two in lockstep across apply-to-resume.
    wrap = (await mcp_client.resume_mcp(user_id, "get_base_resume", {}))["resume"]
    if not wrap or not wrap.get("structured"):
        raise RuntimeError("no uploaded resume on file — upload a resume before tailoring")
    return wrap["structured"], wrap.get("id")


async def _analyses_by_job(user_id: uuid.UUID) -> dict[str, dict]:
    analyses = (await mcp_client.db_mcp(user_id, "get_analyses", {}))["analyses"]
    out: dict[str, dict] = {}
    for a in analyses:
        jid = a.get("job_id")
        ex = a.get("extracted_json") or {}
        if jid and jid not in out:  # newest-first from db-mcp; keep the latest
            out[jid] = {
                **ex,
                "gaps_vs_resume": ex.get("gaps_vs_resume") or (a.get("gaps_json") or {}).get("gaps"),
            }
    return out


async def _jobs_by_id(user_id: uuid.UUID) -> dict[str, dict]:
    jobs = (await mcp_client.db_mcp(user_id, "get_qualified_jobs", {"limit": 5000}))["jobs"]
    return {j["id"]: j for j in jobs}


async def _attested_by_job(user_id: uuid.UUID) -> dict[str, dict]:
    """Per job, the Gap-closer proposals the user ACCEPTED (Phase 6.6). Jobs with no
    proposals, or proposals nobody accepted, are absent — those run the ordinary
    evidence-gated loop with no prompt change at all."""
    suggestions = (await mcp_client.db_mcp(user_id, "get_suggestions", {}))["suggestions"]
    out: dict[str, dict] = {}
    for s in suggestions:
        jid, gc = s.get("job_id"), s.get("gap_close_json")
        if not jid or not gc or gap_closer.is_empty(gc):
            continue
        out[jid] = gap_closer.accepted_items(gc)
    return out


async def _ensure_analyses(user_id: uuid.UUID, job_ids: list[str]) -> dict[str, dict]:
    """Analyses for the selection, analysing any job that doesn't have one yet. The
    Analyzer already scopes to selected ids, so this costs only the missing jobs."""
    analyses = await _analyses_by_job(user_id)
    missing = [jid for jid in job_ids if jid not in analyses]
    if not missing:
        return analyses
    from .analyze_runner import run_analyze

    logger.info("cv_align.analyzing %d selected job(s) with no analysis", len(missing))
    await run_analyze(user_id, job_ids=missing)
    return await _analyses_by_job(user_id)


def _attested_trail(attested: dict | None) -> dict:
    """What the user personally vouched for on this draft, recorded so the review UI and
    the exported audit trail can distinguish it from résumé-grounded content. An empty
    attested set records nothing, keeping legacy/honest drafts byte-identical."""
    att = attested or {}
    skills = [it.get("skill") for it in (att.get("claimed_skills") or [])
              if isinstance(it, dict) and it.get("skill")]
    new_bullets = [{"role_ref": it.get("role_ref"), "text": it.get("text")}
                   for it in (att.get("new_bullets") or []) if isinstance(it, dict)]
    rewrites = [{"role_ref": it.get("role_ref"), "after": it.get("after")}
                for it in (att.get("bullet_rewrites") or []) if isinstance(it, dict)]
    if not (skills or new_bullets or rewrites):
        return {}
    return {
        "user_attested": {
            "skills": skills,
            "new_bullets": new_bullets,
            "bullet_rewrites": rewrites,
            "note": "The candidate confirmed these items themselves; they are not backed by "
                    "résumé citations. Interview prep is on the candidate.",
        }
    }


def _notes(result: dict, cv: dict, base: dict, settings, attested: dict | None = None) -> dict:
    """The review trail persisted on the draft — what was added or rejected, and (when a
    Critic ran) how aligned over how many rounds.

    In single-pass mode the critic keys are **omitted, not zeroed**. The UI hides each of
    them on ``!= null``, so omitting yields "not scored"; writing 0 would render a green-
    to-amber "best 0%" badge and claim the draft scored bottom marks, which is a different
    and false statement. The deterministic gate's findings are recorded either way — that
    is the part that is still evidence, not opinion.
    """
    notes = {
        **_attested_trail(attested),
        "added_skills": cv_fields.added_skills(cv, base),
        "skill_evidence": cv.get("skill_evidence") or [],
        "change_notes": cv.get("change_notes") or [],
    }
    findings = result.get("code_findings") or {}
    if findings:
        notes["code_violations"] = findings.get("violations") or []
        notes["code_rejected_skills"] = findings.get("rejected_skills") or []

    if not result.get("rounds"):
        notes["single_pass"] = True
        return notes

    critique = result.get("critique") or {}
    notes.update({
        "aligned": result["aligned"],
        "alignment_score": result["alignment_score"],
        "threshold": settings.cv_alignment_threshold,
        "rounds_run": len(result["rounds"]),
        "max_rounds": settings.cv_max_rounds,
        "rounds": result["rounds"],
        "final_verdict": critique.get("verdict"),
        "dimension_scores": critique.get("dimension_scores"),
        "matched_requirements": critique.get("matched_requirements") or [],
        "unaddressed_requirements": critique.get("unaddressed_requirements") or [],
        "integrity_flags": critique.get("integrity_flags") or [],
    })
    return notes


async def run_align(user_id: uuid.UUID, job_ids: list[str], *, use_attested: bool = True) -> dict:
    """Align a CV to each selected job. Persists one ``draft_ready`` cv_draft per job.
    Per-job error isolation; bounded selection.

    By default this is a **single Rewriter pass per job** (``cv_max_rounds = 0``) — the
    deterministic gate still runs, but no Critic grades the result, so ``aligned`` is not
    measured and the summary reports ``judged: False``. Setting ``cv_max_rounds`` above 0
    restores the Rewriter⇄Critic loop.

    ``use_attested`` decides whether accepted Gap-closer proposals are folded in. It is on
    for the **Tailor only** path (and the Concierge's tailor tool), which is the reviewed,
    human-gated route the attested flow was built for. The one-click **Prepare CVs** chain
    passes ``False``: it auto-approves and exports without a review stop, so content whose
    only warrant is the candidate's say-so must never ride it. That keeps Prepare CVs
    byte-for-byte the behaviour it had before Phase 6.6.
    """
    settings = get_settings()
    job_ids = list(dict.fromkeys(job_ids))[: settings.cv_tailor_max_jobs]
    if not job_ids:
        return {"aligned": 0, "tailored": 0, "attested": 0, "drafts": [], "errors": {},
                "llm_usage": {}}

    base, base_version = await _base_resume(user_id)
    snapshot = cv_fields.extract_locked_snapshot(base)
    analyses = await _ensure_analyses(user_id, job_ids)
    jobs = await _jobs_by_id(user_id)
    attested_by_job = await _attested_by_job(user_id) if use_attested else {}

    drafts: list[dict] = []
    errors: dict[str, str] = {}
    cost = 0.0
    rounds_total = 0
    fully_aligned = 0
    attested_jobs = 0

    for jid in job_ids:
        job = jobs.get(jid)
        if job is None:
            errors[jid] = "job not found"
            continue
        analysis = analyses.get(jid)
        if not analysis:
            errors[jid] = "no analysis could be produced for this job"
            continue
        attested = attested_by_job.get(jid)
        try:
            result = await align_cv(base, snapshot, job, analysis, settings, attested=attested)
            cv = result["cv"]
            cost += sum(u.cost_usd for u in result["usage"].values())
            rounds_total += len(result["rounds"])
            fully_aligned += 1 if result["aligned"] else 0
            attested_jobs += 1 if attested else 0

            saved = await mcp_client.db_mcp(user_id, "create_cv_draft", {
                "job_id": jid,
                "tailored_content_json": cv,
                "locked_snapshot_json": snapshot,
                "base_resume_version": base_version,
                "critic_notes_json": _notes(result, cv, base, settings, attested),
                "before_score": (job.get("qualification") or {}).get("score"),
            })
            drafts.append({
                "job_id": jid,
                "draft_id": saved["draft"]["id"],
                "aligned": result["aligned"],
                "alignment_score": result["alignment_score"],
                "rounds": len(result["rounds"]),
                "attested": bool(attested),
            })
        except Exception as exc:  # noqa: BLE001 - one bad job must not sink the batch
            errors[jid] = f"{type(exc).__name__}: {exc}"
            logger.warning("cv_align.job id=%s FAILED: %s", jid, exc)

    judged = settings.cv_max_rounds > 0
    summary = {
        "tailored": len(drafts),          # drafts produced (the historical key)
        # Whether a Critic graded anything at all. Without it `aligned: 0` would read as
        # "none cleared the bar" when the truth is "no bar was applied" — so every caller
        # that phrases `aligned` to a human must check this first.
        "judged": judged,
        "aligned": fully_aligned,         # how many cleared the alignment bar (0 if unjudged)
        "attested": attested_jobs,        # of those, how many carried user-attested content
        "drafts": drafts,
        "errors": errors,
        "llm_usage": {
            "rewriter_model": settings.rewriter_model,
            "critic_model": settings.cv_critic_model if judged else None,
            "rounds_total": rounds_total,
            "estimated_cost_usd": round(cost, 6),
        },
    }
    logger.info("cv_align.summary drafts=%d judged=%s aligned=%d attested=%d rounds=%d "
                "errors=%d cost=%.6f", len(drafts), judged, fully_aligned, attested_jobs,
                rounds_total, len(errors), cost)
    return summary


# Backwards-compatible alias: the Celery task, the Concierge tool and the Prepare-CVs
# chain all speak in terms of "tailor", which is now the whole align→format pipeline.
run_tailor = run_align
