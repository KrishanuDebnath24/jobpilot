"""Human-in-the-loop gate configuration (Phase 6, 6a).

A *gate* is a configurable pause point at a pipeline transition. When enabled it
turns Phase 4's LangGraph checkpoint into an **interactive interrupt**: the graph
pauses, emits ``gate_pending``, and waits for ``approve_gate``/``skip_gate`` before
continuing. This module is the pure policy layer — which transitions can be gated,
their defaults, and the reconcile-safe decision function. No DB, no LLM, no graph.

Defaults (Phase 6 decision): the autonomous Scout→Qualify pipeline runs through
without pausing (``after_scout`` / ``after_qualify`` OFF); analysis/enhance gates
are OFF; **apply-to-resume is gated by default** (it changes a user-facing artifact).
"""

from __future__ import annotations

# Gate keys are named for the transition they guard. ``after_<stage>`` pauses once
# that stage's row is terminal, before the next stage triggers. ``before_apply`` is
# the apply-to-resume approval gate (6e) — not a pipeline stage, enforced at write.
GATE_KEYS: tuple[str, ...] = ("after_scout", "after_qualify", "after_analyze", "before_apply")

GATE_DEFAULTS: dict[str, bool] = {
    "after_scout": False,   # pipeline runs autonomously through scout→qualify…
    "after_qualify": False,  # …by default (Phase 4 behaviour preserved)
    "after_analyze": False,  # analysis/enhance gates off by default
    "before_apply": True,    # apply-to-resume changes an artifact → gated by default
}

# Which gate (if any) fires immediately AFTER a given pipeline stage completes.
STAGE_TO_GATE: dict[str, str] = {
    "scout": "after_scout",
    "qualify": "after_qualify",
    "analyze": "after_analyze",
}

# Terminal statuses a gate row can hold once a decision has been recorded. The gate
# node reconciles against these (persisted truth) rather than hanging on an event.
GATE_RESOLVED = ("approved", "skipped")
GATE_PENDING = "pending"

# A gate is persisted as a pipeline_runs row with this stage prefix, so the
# orchestrator reconciles a gate decision the same way it reconciles a stage.
GATE_STAGE_PREFIX = "gate:"


def gate_stage(gate_key: str) -> str:
    return f"{GATE_STAGE_PREFIX}{gate_key}"


def effective_gates(stored: dict | None) -> dict[str, bool]:
    """Merge a user's stored overrides onto the defaults. Only known gate keys are
    honoured; anything else in ``stored`` is ignored (a caller can't invent gates)."""
    gates = dict(GATE_DEFAULTS)
    for key, value in (stored or {}).items():
        if key in GATE_DEFAULTS:
            gates[key] = bool(value)
    return gates


def is_gate_enabled(gates: dict | None, gate_key: str) -> bool:
    return bool(effective_gates(gates).get(gate_key, False))


def gate_after_stage(gates: dict | None, stage: str) -> str | None:
    """Return the gate key that should pause the pipeline after ``stage`` completes,
    or ``None`` if that transition isn't gated for this user."""
    gate_key = STAGE_TO_GATE.get(stage)
    if gate_key and is_gate_enabled(gates, gate_key):
        return gate_key
    return None


def gate_decision(enabled: bool, persisted_status: str | None) -> str:
    """The reconcile-safe core of the gate node. Given whether the gate is enabled
    and the persisted status of *this* gate row, decide what to do:

    - ``"proceed"``  — gate off, or already approved/skipped (a resumed/replayed
      pipeline must NOT re-pause on a gate the user already cleared);
    - ``"pause"``    — gate on and not yet resolved → interrupt and wait.

    This never trusts an in-flight event: the persisted row is the source of truth,
    exactly like Phase 4's stage reconciliation."""
    if not enabled:
        return "proceed"
    if persisted_status in GATE_RESOLVED:
        return "proceed"
    return "pause"


def gate_id(pipeline_id: str, gate_key: str) -> str:
    """Opaque handle the UI/Concierge pass to approve_gate/skip_gate."""
    return f"{pipeline_id}:{gate_key}"


def parse_gate_id(value: str) -> tuple[str, str]:
    """Inverse of :func:`gate_id` → ``(pipeline_id, gate_key)``."""
    pipeline_id, _, gate_key = value.partition(":")
    if not pipeline_id or gate_key not in GATE_KEYS:
        raise ValueError(f"invalid gate_id {value!r}")
    return pipeline_id, gate_key
