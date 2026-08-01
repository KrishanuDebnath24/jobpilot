"""Apply-to-resume orchestration (Phase 6, 6e).

Reads the latest resume (resume-mcp) + the user's ACCEPTED suggestions (db-mcp),
assembles a new version with the honest, non-fabricating rules in
``jobpilot_core.resume_apply``, and — on apply — persists it as a NEW resume version
(resume-mcp), preserving the original. ``prepare`` is read-only (the gate preview);
``apply`` writes. ``user_id`` comes from the trusted context; resume content is never
logged (counts/skill names only).
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core import resume_apply

from . import mcp_client

logger = logging.getLogger("jobpilot.apply")


async def _load(user_id: uuid.UUID) -> tuple[dict, list[dict], str | None]:
    """Base = the latest *uploaded* resume, never a previous assembled version.

    Applying twice therefore re-derives from the same source instead of compounding on
    its own output: each assembled version is an independent render of (upload + accepted
    suggestions), which is what makes the previous one safely disposable. Building on the
    last assembled version would also silently drop rewrites, since their ``before`` text
    no longer matches once an earlier pass has already rewritten it."""
    wrap = (await mcp_client.resume_mcp(user_id, "get_base_resume", {}))["resume"]
    if not wrap or not wrap.get("structured"):
        raise RuntimeError("no uploaded resume on file — upload a resume before applying suggestions")
    suggestions = (await mcp_client.db_mcp(user_id, "get_suggestions", {}))["suggestions"]
    accepted = [s for s in suggestions if s.get("status") == "accepted"]
    return wrap["structured"], accepted, wrap.get("id")


async def prepare_apply(
    user_id: uuid.UUID, confirmed_skills: list[str] | None = None
) -> dict:
    """Assemble the preview WITHOUT writing. Shows applied rewrites, added skills, and
    which skills are withheld pending confirmation (the honesty guard)."""
    resume, accepted, parent_id = await _load(user_id)
    preview = resume_apply.apply_suggestions(resume, accepted, confirmed_skills)
    logger.info(
        "apply.prepare accepted=%d rewrites=%d added=%d withheld=%d",
        len(accepted), len(preview["applied_rewrites"]),
        len(preview["added_skills"]), len(preview["withheld_skills"]),
    )
    return {
        "accepted_count": len(accepted),
        "applied_rewrites": preview["applied_rewrites"],
        "added_skills": preview["added_skills"],
        "withheld_skills": preview["withheld_skills"],   # confirm these to include them
        "project_guidance": preview["project_guidance"],  # guidance only, not inserted
        "parent_id": parent_id,
    }


async def apply_to_resume(
    user_id: uuid.UUID, confirmed_skills: list[str] | None = None, label: str | None = None
) -> dict:
    """Assemble and PERSIST a new resume version from accepted suggestions. Original is
    preserved (new row). Returns the new version id + a non-sensitive summary."""
    resume, accepted, parent_id = await _load(user_id)
    if not accepted:
        return {"applied": False, "reason": "no accepted suggestions to apply"}
    result = resume_apply.apply_suggestions(resume, accepted, confirmed_skills)
    raw_text = resume_apply.render_resume_text(result["resume"])
    created = await mcp_client.resume_mcp(user_id, "create_resume_version", {
        "structured": result["resume"],
        "raw_text": raw_text,
        "label": label or "Tailored from accepted suggestions",
        "parent_id": parent_id,
    })
    # Keep exactly one assembled version: the one just written. Older ones are stale
    # renders of the same upload + the same accepted set, so nothing is lost that this
    # version doesn't already contain. Only after the new row exists, so a failure here
    # leaves the user with more versions rather than none. Uploads are never eligible.
    new_id = (created.get("resume") or {}).get("id")
    pruned = 0
    if new_id:
        pruned = (await mcp_client.resume_mcp(
            user_id, "delete_assembled_versions", {"keep_id": new_id}
        )).get("deleted", 0)
    logger.info("apply.done resume_id=%s rewrites=%d added=%d withheld=%d pruned=%d",
                new_id, len(result["applied_rewrites"]),
                len(result["added_skills"]), len(result["withheld_skills"]), pruned)
    return {
        "applied": True,
        "resume": created.get("resume"),
        "applied_rewrites": len(result["applied_rewrites"]),
        "added_skills": result["added_skills"],
        "withheld_skills": result["withheld_skills"],
        "project_guidance_count": len(result["project_guidance"]),
        "pruned_versions": pruned,
    }
