"""Per-draft edits from the review UI (Phase 6.6) — the HTTP twin of the Concierge's
``edit_cv``/``reword_cv`` chat tools.

Same two operations, same guarantees, reached by clicking instead of typing:

* :func:`edit_draft` — structural add/remove of skills and bullets. Deterministic, NO LLM,
  zero tokens. Adding a skill the résumé doesn't evidence is recorded as
  ``cv_fields.USER_CONFIRMED``, so the trail says plainly that the candidate is the
  guarantor rather than passing it off as résumé-grounded.
* :func:`reword_draft` — one focused model call rewording the summary or a single role's
  bullets to a free-text instruction. Cheaper than re-running the whole alignment loop,
  which is the point: a wording tweak shouldn't cost a full re-tailor.

Both route the result through ``cv_fields.finalize`` (locked facts re-imposed, evidence gate
applied, headers carried) and persist via ``set_cv_draft_content``, which resets the draft
to ``draft_ready`` and detaches the stale PDF/DOCX — so an edit can never silently ship
inside an already-exported file. Reads/writes only via MCP.
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import cv_edit, cv_fields, cv_rewriter
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.cv_draft_edit")


async def _load(user_id: uuid.UUID, draft_id: str) -> tuple[dict, dict, dict]:
    """``(draft, base_resume, snapshot)`` for an edit. Raises if the draft isn't the
    caller's — db-mcp scopes every read to the trusted user id."""
    got = await mcp_client.db_mcp(user_id, "get_cv_draft", {"draft_id": draft_id})
    draft = got.get("draft") if isinstance(got, dict) else None
    if not draft:
        raise LookupError("draft not found")
    wrap = (await mcp_client.resume_mcp(user_id, "get_base_resume", {}))["resume"] or {}
    base = wrap.get("structured") or {}
    # Prefer the snapshot captured when the draft was made: it pins the locked facts as they
    # were at tailor time, so a résumé re-upload can't retro-edit an existing draft.
    snapshot = draft.get("locked_snapshot_json") or cv_fields.extract_locked_snapshot(base)
    return draft, base, snapshot


async def _persist(user_id: uuid.UUID, draft_id: str, content: dict) -> None:
    await mcp_client.db_mcp(user_id, "set_cv_draft_content",
                            {"draft_id": draft_id, "tailored_content_json": content})


async def edit_draft(
    user_id: uuid.UUID,
    draft_id: str,
    *,
    add_skills: list[str] | None = None,
    remove_skills: list[str] | None = None,
    add_bullets: list[dict] | None = None,
    remove_bullets: list[str] | None = None,
) -> dict:
    """Apply structural edits to one draft. Returns ``{status, changes, warnings, content}``.
    ``status`` is ``no_change`` when nothing matched, so the UI can say so instead of
    claiming a save that didn't happen."""
    draft, base, snapshot = await _load(user_id, draft_id)
    out = cv_edit.edit_tailored_cv(
        draft.get("tailored_content_json") or {}, base, snapshot,
        add_skills=add_skills, remove_skills=remove_skills,
        add_bullets=add_bullets, remove_bullets=remove_bullets)
    if not out["changes"]:
        return {"status": "no_change", "changes": [],
                "warnings": out["warnings"] or ["nothing matched that edit"],
                "content": draft.get("tailored_content_json") or {}}
    await _persist(user_id, draft_id, out["content"])
    logger.info("cv_draft_edit.edit draft=%s changes=%d warnings=%d",
                draft_id, len(out["changes"]), len(out["warnings"]))
    return {"status": "edited", "changes": out["changes"], "warnings": out["warnings"],
            "content": out["content"]}


async def reword_draft(
    user_id: uuid.UUID,
    draft_id: str,
    *,
    target: str,
    instruction: str,
    role_index: int | None = None,
    role_ref: str | None = None,
) -> dict:
    """Reword the summary (``target='summary'``) or one role's bullets (``'role'``) to
    ``instruction``. One model call; the result is spliced in and re-finalized."""
    if not (instruction or "").strip():
        return {"status": "no_change", "changes": [], "warnings": ["no instruction given"]}
    draft, base, snapshot = await _load(user_id, draft_id)
    content = draft.get("tailored_content_json") or {}
    target = "role" if (target or "").lower() == "role" else "summary"

    idx = 0
    if target == "role":
        exp = content.get("experience") or []
        resolved = cv_edit._match_role(exp, role_ref, role_index)
        if resolved is None:
            return {"status": "no_change", "changes": [],
                    "warnings": ["couldn't tell which role to reword — name the employer"]}
        idx = resolved

    analyses = (await mcp_client.db_mcp(user_id, "get_analyses", {}))["analyses"]
    analysis = next((a.get("extracted_json") or {} for a in analyses
                     if a.get("job_id") == draft.get("job_id")), {})
    jobs = (await mcp_client.db_mcp(user_id, "get_qualified_jobs", {"limit": 5000}))["jobs"]
    job = next((j for j in jobs if j["id"] == draft.get("job_id")), {})

    res = await cv_rewriter.reword_section(
        base, snapshot, job, analysis, content, target=target,
        instruction=instruction, role_index=idx, settings=get_settings())
    data = res.data or {}

    spliced = dict(content)
    if target == "summary" and data.get("summary"):
        spliced["summary"] = data["summary"]
    elif target == "role" and data.get("bullets"):
        exp = [dict(e) for e in (spliced.get("experience") or [])]
        if idx < len(exp):
            exp[idx]["bullets"] = data["bullets"]
            spliced["experience"] = exp
    else:
        return {"status": "no_change", "changes": [],
                "warnings": ["the rewriter returned nothing usable — try again"]}

    final = cv_fields.finalize(spliced, base, snapshot)
    await _persist(user_id, draft_id, final)
    what = "summary" if target == "summary" else \
        f"bullets for {(content.get('experience') or [{}])[idx].get('company') or f'role {idx}'}"
    logger.info("cv_draft_edit.reword draft=%s target=%s", draft_id, target)
    return {"status": "edited", "changes": [f"reworded the {what}"], "warnings": [],
            "content": final}
