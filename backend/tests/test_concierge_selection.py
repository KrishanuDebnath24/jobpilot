"""Concierge acting on the dashboard SELECTION + job deletion (Phase 6 extension).

Deterministic: the MCP dispatch is a fake ``call``, so this proves the executor's
selection resolution, confirmation gating, and routing — not model behaviour.

The bars:
  * "act on selected" resolves ``use_selected`` to the executor's selection, and the
    confirmation prompt names the real count;
  * a confirmed delete fires on exactly the resolved ids, through db-mcp;
  * explicit ``job_ids`` always win over the selection;
  * "act on selected" with nothing selected is a no-op, not a confirm-and-wipe;
  * the new paid/destructive tools are all confirmation-gated.
"""

from __future__ import annotations

import uuid

import pytest
from jobpilot_core import concierge


def _executor(selected=None):
    from app.concierge_runner import ConciergeExecutor

    calls: list[tuple] = []

    async def fake_call(user_id, service, tool, args):
        calls.append((service, tool, dict(args)))
        if tool == "delete_jobs":
            return {"deleted": len(args.get("job_ids") or [])}
        if tool == "delete_disqualified_jobs":
            return {"deleted": 3}
        if tool == "delete_unscored_jobs":
            return {"deleted": 5}
        if tool == "delete_all_jobs":
            return {"deleted": 9}
        return {}

    return ConciergeExecutor(uuid.uuid4(), call=fake_call, selected_job_ids=selected), calls


# --------------------------------------------------------------------------- #
# Selection resolution
# --------------------------------------------------------------------------- #
async def test_delete_selected_holds_for_confirmation_and_names_the_count():
    ex, calls = _executor(selected=["j1", "j2", "j3"])
    result, _ = await ex.execute("delete_jobs", {"use_selected": True}, confirmed=False)
    assert result["status"] == "confirmation_required"
    assert "3" in result["prompt"]                       # the real count, not "the selected"
    assert result["resolved_args"]["job_ids"] == ["j1", "j2", "j3"]
    assert not calls                                     # nothing deleted while awaiting yes


async def test_confirmed_delete_fires_on_the_resolved_ids():
    ex, calls = _executor(selected=["j1", "j2"])
    # run_turn stores resolved_args as the pending args; simulate the confirmed run.
    result, _ = await ex.execute("delete_jobs", {"job_ids": ["j1", "j2"]}, confirmed=True)
    assert result["deleted"] == 2
    assert ("db", "delete_jobs", {"job_ids": ["j1", "j2"]}) in calls


async def test_explicit_job_ids_win_over_selection():
    ex, _ = _executor(selected=["sel1", "sel2"])
    result, _ = await ex.execute(
        "delete_jobs", {"job_ids": ["explicit1"], "use_selected": True}, confirmed=False)
    assert result["resolved_args"]["job_ids"] == ["explicit1"]  # explicit ids are not overridden


async def test_act_on_selected_with_empty_selection_is_a_no_op():
    ex, calls = _executor(selected=[])
    result, _ = await ex.execute("delete_jobs", {"use_selected": True}, confirmed=False)
    assert result["status"] == "no_selection"            # not confirmation_required
    assert not calls


@pytest.mark.parametrize("tool", ["tailor_cvs", "prepare_cvs", "trigger_stage"])
async def test_selection_resolves_across_the_job_tools(tool):
    ex, _ = _executor(selected=["j1"])
    args = {"use_selected": True}
    if tool == "trigger_stage":
        args["stage"] = "analyze"
    result, _ = await ex.execute(tool, args, confirmed=False)
    assert result["status"] == "confirmation_required"   # all paid → held first
    assert result["resolved_args"]["job_ids"] == ["j1"]


async def test_trigger_stage_resolves_selection_before_confirming():
    # trigger_stage(analyze) now runs the runner synchronously on confirm, so verify the
    # selection is resolved into resolved_args at the confirmation step (before any run).
    ex, calls = _executor(selected=["j1", "j2"])
    result, _ = await ex.execute("trigger_stage", {"stage": "analyze", "use_selected": True}, confirmed=False)
    assert result["status"] == "confirmation_required"
    assert result["resolved_args"]["stage"] == "analyze"
    assert result["resolved_args"]["job_ids"] == ["j1", "j2"]
    assert not calls  # nothing runs while awaiting the user's yes


# --------------------------------------------------------------------------- #
# Deletion of disqualified jobs
# --------------------------------------------------------------------------- #
async def test_delete_disqualified_confirms_then_runs():
    ex, calls = _executor()
    held, _ = await ex.execute("delete_disqualified_jobs", {}, confirmed=False)
    assert held["status"] == "confirmation_required" and not calls
    done, _ = await ex.execute("delete_disqualified_jobs", {}, confirmed=True)
    assert done["deleted"] == 3
    assert ("db", "delete_disqualified_jobs") in [(s, t) for s, t, _ in calls]


# --------------------------------------------------------------------------- #
# Policy: the new tools are all consequential (paid or destructive)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool", ["prepare_cvs", "delete_jobs", "delete_disqualified_jobs",
                                 "delete_unscored_jobs", "delete_all_jobs"])
def test_new_tools_are_confirmation_gated(tool):
    assert tool in concierge.CONSEQUENTIAL
    assert concierge.authorize(tool, confirmed=False) == "confirm"
    assert concierge.authorize(tool, confirmed=True) == "execute"


def test_new_tools_are_registered():
    for tool in ("prepare_cvs", "delete_jobs", "delete_disqualified_jobs",
                 "delete_unscored_jobs", "delete_all_jobs"):
        assert tool in concierge.TOOL_NAMES


# --------------------------------------------------------------------------- #
# Bulk deletes: the unscored bucket, and emptying the pool
# --------------------------------------------------------------------------- #
async def test_delete_unscored_confirms_then_runs():
    ex, calls = _executor()
    held, _ = await ex.execute("delete_unscored_jobs", {}, confirmed=False)
    assert held["status"] == "confirmation_required" and not calls
    done, _ = await ex.execute("delete_unscored_jobs", {}, confirmed=True)
    assert done["deleted"] == 5
    assert ("db", "delete_unscored_jobs") in [(s, t) for s, t, _ in calls]


async def test_delete_all_jobs_confirms_then_runs():
    ex, calls = _executor()
    held, _ = await ex.execute("delete_all_jobs", {}, confirmed=False)
    assert held["status"] == "confirmation_required" and not calls
    done, _ = await ex.execute("delete_all_jobs", {}, confirmed=True)
    assert done["deleted"] == 9
    assert ("db", "delete_all_jobs") in [(s, t) for s, t, _ in calls]


def test_emptying_the_pool_needs_an_erasure_word_not_a_bare_yes():
    """Emptying the pool throws away every paid scout/qualify/analyze run, so it sits in
    DESTRUCTIVE with full erasure: a stray "sure" — the kind a user fires at whatever the
    Concierge last offered — must not clear it. The narrower deletes stay on a plain yes,
    or routine tidying becomes a chore."""
    assert concierge.is_destructive("delete_all_jobs")
    assert not concierge.confirms_action("delete_all_jobs", "yes")
    assert not concierge.confirms_action("delete_all_jobs", "sure, go ahead")
    assert concierge.confirms_action("delete_all_jobs", "yes, delete them")
    # ...and negation still wins over the erasure word.
    assert not concierge.confirms_action("delete_all_jobs", "no, don't delete them")
    for narrower in ("delete_jobs", "delete_disqualified_jobs", "delete_unscored_jobs"):
        assert not concierge.is_destructive(narrower)
        assert concierge.confirms_action(narrower, "yes")


def test_bulk_delete_prompts_say_what_survives():
    """The two buckets are complements, and the prompt is the only place the user sees
    which one they are about to lose — so each has to name what it keeps."""
    unscored = concierge.confirmation_prompt("delete_unscored_jobs", {})
    assert "UNSCORED" in unscored and "kept" in unscored.lower()
    all_jobs = concierge.confirmation_prompt("delete_all_jobs", {})
    assert "ALL" in all_jobs and "cannot be undone" in all_jobs
    # It empties the pool, not the account — the prompt must not read like GDPR erasure.
    assert "resume" in all_jobs.lower()
