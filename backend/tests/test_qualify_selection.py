"""Which jobs a qualify run actually scores.

Qualify is a paid stage, so its selection rule is a spend decision as much as a
correctness one. It used to skip a job only while the job's recorded scoring key —
effective CV + qualifier config — still matched the current one. That reads as
"incremental", but the CV half of the key is the *resume row id*, and re-uploading a CV
mints a new row. So every upload silently invalidated every score in the pool, and the
next qualify re-ran and re-billed work that had already been done: a pool of 153 with 107
already scored charged for all 153.

The rule is now simply: **a job that has been scored is never scored again** unless the
caller explicitly asks for a full re-score. The bars:

  * an already-scored job is skipped whatever its recorded CV/config version — including
    the case that used to trigger the re-run, a new resume id;
  * a job with no qualification is always scored;
  * `requalify_all=True` still does the whole pool, so a tightened config has a way out;
  * a scored-but-undecided job (score written, `decision` still NULL) counts as scored —
    the filter is the presence of the row, matching `delete_unscored_jobs`.

Offline: the MCP dispatch is faked, so this pins the runner's selection, not scoring.
"""

from __future__ import annotations

import uuid

import pytest


def _job(jid: str, qualification=None) -> dict:
    return {"id": jid, "title": "Engineer", "company": "Acme",
            "description": "Go, Kubernetes, distributed systems",
            "qualification": qualification}


def _scored(cv_version: str = "resume-1", config_version: str = "cfg-1") -> dict:
    return {"score": 0.7, "decision": "qualified",
            "reasons_json": {"cv_version": cv_version, "config_version": config_version}}


class _Usage:
    input_tokens = output_tokens = 0
    cost_usd = 0.0


class _Refined:
    """Stands in for the Haiku refine pass: gate-passers would otherwise make a real API
    call, and this suite is about which jobs get picked, not how they score."""

    data = {"dimension_scores": {}, "reasons": [], "roles_match": True}
    usage = _Usage()


async def _run(jobs: list[dict], *, requalify_all: bool = False, current_resume="resume-1"):
    """Drive run_qualify over a faked MCP surface; return (summary, scored_job_ids)."""
    from app import qualify_runner

    scored: list[str] = []

    async def fake_db(user_id, tool, args=None):
        if tool == "get_qualified_jobs":
            return {"jobs": jobs}
        if tool == "get_cv_drafts":
            return {"drafts": []}
        if tool == "upsert_qualification":
            scored.append((args or {})["job_id"])
            return {}
        return {}

    async def fake_resume(user_id, tool, args=None):
        return {"resume": {"id": current_resume,
                           "structured": {"skills": ["Go"], "experience": [], "summary": "Eng"}}}

    async def fake_config(user_id, tool, args=None):
        return {"config": {"id": "cfg-1", "weights": {}, "threshold": 0.5, "must_have": []}}

    async def fake_refine(*a, **kw):
        return _Refined()

    async def no_emit(*a, **kw):
        """The event bus talks to Redis; liveness is not what is under test here."""

    import app.mcp_client as mc

    orig = (mc.db_mcp, mc.resume_mcp, mc.config_mcp)
    orig_refine, orig_emit = qualify_runner.refine_with_llm, qualify_runner._emit
    qualify_runner.refine_with_llm, qualify_runner._emit = fake_refine, no_emit
    mc.db_mcp, mc.resume_mcp, mc.config_mcp = fake_db, fake_resume, fake_config
    qualify_runner.mcp_client.db_mcp = fake_db
    qualify_runner.mcp_client.resume_mcp = fake_resume
    qualify_runner.mcp_client.config_mcp = fake_config
    try:
        summary = await qualify_runner.run_qualify(uuid.uuid4(), requalify_all=requalify_all)
    finally:
        mc.db_mcp, mc.resume_mcp, mc.config_mcp = orig
        qualify_runner.mcp_client.db_mcp, qualify_runner.mcp_client.resume_mcp, \
            qualify_runner.mcp_client.config_mcp = orig
        qualify_runner.refine_with_llm, qualify_runner._emit = orig_refine, orig_emit
    return summary, scored


async def test_only_unscored_jobs_are_scored():
    summary, scored = await _run([
        _job("new-1"),
        _job("done-1", _scored()),
        _job("new-2"),
        _job("done-2", _scored()),
    ])
    assert sorted(scored) == ["new-1", "new-2"]
    assert summary["skipped"] == 2


async def test_a_new_resume_upload_does_not_re_bill_the_pool():
    """The regression this rule exists for: the stored key names the OLD resume id, which
    is exactly the state a re-upload leaves every scored job in. Before, that re-scored the
    whole pool; a 153-job pool with 107 done charged for 153."""
    jobs = [_job(f"done-{i}", _scored(cv_version="resume-OLD")) for i in range(107)]
    jobs += [_job(f"new-{i}") for i in range(46)]
    summary, scored = await _run(jobs, current_resume="resume-NEW")
    assert len(scored) == 46, "a re-upload re-billed jobs that were already scored"
    assert summary["skipped"] == 107


async def test_a_changed_config_also_leaves_scored_jobs_alone():
    """Staleness is no longer chased at all — not for the CV, not for the config. The
    escape hatch is explicit, so this must not quietly re-score either."""
    _, scored = await _run([_job("done", _scored(config_version="cfg-OLD")), _job("new")])
    assert scored == ["new"]


async def test_requalify_all_still_does_the_whole_pool():
    """The way out for a tightened config or a genuinely better CV. Without this the new
    rule would make existing scores permanently unrefreshable."""
    summary, scored = await _run(
        [_job("a", _scored()), _job("b", _scored()), _job("c")], requalify_all=True)
    assert sorted(scored) == ["a", "b", "c"]
    assert summary["skipped"] == 0


async def test_a_scored_job_with_no_decision_still_counts_as_scored():
    """Qualify can write a score before it has a verdict. That job has been looked at and
    paid for, so 'never scored' is not true of it — same rule delete_unscored_jobs uses."""
    _, scored = await _run([
        _job("undecided", {"score": 0.4, "decision": None, "reasons_json": {}}),
        _job("fresh"),
    ])
    assert scored == ["fresh"]


@pytest.mark.parametrize("qualification", [None, {}])
async def test_a_job_with_no_qualification_is_always_scored(qualification):
    _, scored = await _run([_job("j", qualification)])
    assert scored == ["j"]
