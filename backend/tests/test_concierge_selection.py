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


async def test_trigger_stage_strips_use_selected_before_dispatch():
    ex, calls = _executor(selected=["j1", "j2"])
    await ex.execute("trigger_stage", {"stage": "analyze", "use_selected": True}, confirmed=True)
    dispatched = [a for s, t, a in calls if t == "trigger_stage"][0]
    assert "use_selected" not in dispatched              # not a real MCP arg
    assert dispatched["stage"] == "analyze" and dispatched["job_ids"] == ["j1", "j2"]


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
@pytest.mark.parametrize("tool", ["prepare_cvs", "delete_jobs", "delete_disqualified_jobs"])
def test_new_tools_are_confirmation_gated(tool):
    assert tool in concierge.CONSEQUENTIAL
    assert concierge.authorize(tool, confirmed=False) == "confirm"
    assert concierge.authorize(tool, confirmed=True) == "execute"


def test_new_tools_are_registered():
    for tool in ("prepare_cvs", "delete_jobs", "delete_disqualified_jobs"):
        assert tool in concierge.TOOL_NAMES
