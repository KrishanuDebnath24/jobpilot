"""CV alignment — one Rewriter pass, optionally graded by a Critic until it matches.

``settings.cv_max_rounds`` picks the mode, and it is the dominant cost lever on this path.

**Single pass (``cv_max_rounds = 0``, the default).** One Rewriter call, the code gate,
done — 1 LLM call per job:

    rewrite ──► [code gate] ──► draft

**Loop (``cv_max_rounds = n > 0``).** A Critic grades each draft and the Rewriter revises
until it clears the bar or the cap is hit — 1 + 2n calls per job worst case:

    rewrite ──► [code gate] ──► critique ──► aligned? ──► done
       ▲                                        │
       └──────────── issues + score ◄───────────┘

**The code gate runs in both modes, and it is what makes the output honest.**
``cv_fields.finalize`` re-imposes the locked snapshot verbatim, drops any added skill
whose citation isn't verifiable in the real resume, and carries every original header
through. Dropping the Critic costs *judgement*, not *integrity*: no un-evidenced skill can
survive either way. What the Critic adds is the question code cannot answer — does the
cited evidence actually *support* the claim? — which in single-pass mode falls to the
human at the review-and-approve step.

In loop mode, exit is :func:`cv_critic.is_aligned` — verdict ``aligned``, score ≥
``cv_alignment_threshold``, and no integrity flag. If it never clears the bar it returns
the **best-scoring** round rather than the last one, marked ``aligned: False``, so a draft
that peaked in round 2 isn't lost to a weaker round 3.

Single pass returns ``aligned``/``alignment_score`` as **None** — no verdict was sought,
which is not the same as failing one — plus ``code_findings`` for what the gate caught
(in loop mode that detail lives per-round in ``rounds``). The caller must not coerce those
Nones to 0/False: an unscored draft is not a zero-scoring draft.

No I/O and no persistence here — the caller owns both.
"""

from __future__ import annotations

import logging

from . import cv_edit, cv_fields
from .cv_critic import critique_cv, is_aligned
from .cv_rewriter import rewrite_cv
from .llm import LLMUsage
from .settings import Settings

logger = logging.getLogger("jobpilot.cv_loop")


def _code_findings(draft: dict, base_resume: dict, snapshot: dict) -> dict:
    """What the deterministic gate caught this round, for the Critic's context."""
    locked = cv_fields.validate_locked(draft, snapshot)
    evidence = cv_fields.validate_skill_evidence(draft, base_resume)
    return {
        "violations": locked["violations"],
        "rejected_skills": [r["skill"] for r in evidence["rejected"]],
        "rejected_detail": evidence["rejected"],
    }


def _gate(draft: dict, base_resume: dict, snapshot: dict, attested: dict | None) -> dict:
    """The deterministic honesty gate: locked facts re-imposed, un-evidenced added skills
    dropped. On the attested path the candidate's confirmed items are forced in first —
    they carry no résumé citation, so without their ``user_confirmed`` basis the evidence
    check inside ``finalize`` would delete them."""
    return (cv_edit.apply_attested(draft, base_resume, snapshot, attested) if attested
            else cv_fields.finalize(draft, base_resume, snapshot))


def _round_record(n: int, critique: dict, findings: dict, cv: dict, base_resume: dict) -> dict:
    """Per-round audit trail persisted on the draft. CV content is summarised, not dumped."""
    return {
        "round": n,
        "verdict": critique.get("verdict"),
        "alignment_score": critique.get("alignment_score"),
        "dimension_scores": critique.get("dimension_scores"),
        "unaddressed_requirements": critique.get("unaddressed_requirements") or [],
        "issues": critique.get("issues") or [],
        "integrity_flags": critique.get("integrity_flags") or [],
        "code_violations": findings["violations"],
        "code_rejected_skills": findings["rejected_skills"],
        "added_skills": cv_fields.added_skills(cv, base_resume),
    }


async def align_cv(
    base_resume: dict,
    snapshot: dict,
    job: dict,
    analysis: dict,
    settings: Settings,
    *,
    attested: dict | None = None,
) -> dict:
    """Run the rewrite⇄critique loop for one job.

    ``attested`` carries Gap-closer proposals the candidate accepted item by item. Both
    agents receive it: the Rewriter as content to weave in, the Critic as claims whose
    support is already settled. It MUST reach the Critic — an attested claim has no résumé
    citation, so a Critic that hasn't been told would flag it as overclaiming, and
    :func:`cv_critic.is_aligned` treats any integrity flag as a hard block.

    Returns ``{cv, aligned, alignment_score, rounds, critique, code_findings, usage}``
    where ``cv`` has already been through the code gate and is safe to persist and export.
    With ``cv_max_rounds = 0`` no Critic runs and ``aligned``/``alignment_score`` are None.
    """
    threshold = settings.cv_alignment_threshold
    max_rounds = max(0, settings.cv_max_rounds)   # 0 = single pass, no Critic
    rw_usage = LLMUsage(model=settings.rewriter_model)
    cr_usage = LLMUsage(model=settings.cv_critic_model)

    def _track(usage: LLMUsage, res) -> None:
        usage.add(res.usage.input_tokens, res.usage.output_tokens,
                  cache_read=res.usage.cache_read_input_tokens,
                  cache_creation=res.usage.cache_creation_input_tokens)

    res = await rewrite_cv(base_resume, snapshot, job, analysis, settings, attested=attested)
    _track(rw_usage, res)
    if not res.data:
        raise RuntimeError("rewriter returned no CV")
    draft = res.data

    if max_rounds == 0:
        # Single pass. The gate still runs, so the draft is as honest as any looped one —
        # it is simply ungraded, and goes to the human unscored rather than to the Critic.
        findings = _code_findings(draft, base_resume, snapshot)
        cv = _gate(draft, base_resume, snapshot, attested)
        logger.info("cv_loop.single_pass job=%s rejected_skills=%d violations=%d",
                    job.get("id"), len(findings["rejected_skills"]), len(findings["violations"]))
        return {
            "cv": cv,
            # None, not False: nothing judged this draft, which is not the same as judging
            # it unaligned. Callers render "not scored", never "scored 0".
            "aligned": None,
            "alignment_score": None,
            "rounds": [],
            "critique": {},
            "code_findings": findings,
            "usage": {"rewriter": rw_usage, "critic": cr_usage},
        }

    rounds: list[dict] = []
    best: tuple[float, dict, dict, dict] | None = None  # (score, cv, critique, findings)

    for n in range(1, max_rounds + 1):
        # Make the draft honest BEFORE it is judged, and tell the Critic what code caught.
        findings = _code_findings(draft, base_resume, snapshot)
        cv = _gate(draft, base_resume, snapshot, attested)

        cres = await critique_cv(base_resume, snapshot, job, analysis, cv, settings,
                                 code_findings=findings, attested=attested)
        _track(cr_usage, cres)
        critique = cres.data or {}
        rounds.append(_round_record(n, critique, findings, cv, base_resume))

        raw_score = critique.get("alignment_score")
        score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
        if best is None or score > best[0]:
            best = (score, cv, critique, findings)

        if is_aligned(critique, threshold):
            logger.info("cv_loop.aligned job=%s round=%d score=%.2f", job.get("id"), n, score)
            return {
                "cv": cv,
                "aligned": True,
                "alignment_score": score,
                "rounds": rounds,
                "critique": critique,
                "code_findings": findings,
                "usage": {"rewriter": rw_usage, "critic": cr_usage},
            }

        if n == max_rounds:
            break

        res = await rewrite_cv(base_resume, snapshot, job, analysis, settings,
                               critique=critique, previous=cv, attested=attested)
        _track(rw_usage, res)
        if not res.data:  # keep the best draft we already have rather than failing the job
            logger.warning("cv_loop.rewrite empty job=%s round=%d — stopping early",
                           job.get("id"), n)
            break
        draft = res.data

    # At least one round always ran here: max_rounds >= 1 on this path (0 returned above).
    score, cv, critique, findings = best
    logger.info("cv_loop.exhausted job=%s rounds=%d best_score=%.2f (below %.2f)",
                job.get("id"), len(rounds), score, threshold)
    return {
        "cv": cv,
        "aligned": False,
        "alignment_score": score,
        "rounds": rounds,
        "critique": critique,
        "code_findings": findings,   # the winning round's, not the last round's
        "usage": {"rewriter": rw_usage, "critic": cr_usage},
    }
