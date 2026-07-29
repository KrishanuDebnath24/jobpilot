"""Mass CV export (Phase 6.5, 6.5e) — deterministic render, runs in the Celery worker.

Runs the **Tailor** (``jobpilot_core.tailor``, no LLM) over every **approved** aligned
draft to lay it out as an A4 Irish-format, ATS-safe **PDF + DOCX**, verifies the PDF is
machine-readable (real selectable text, not an image), stores the files via resume-mcp
(under the user's storage prefix, so erasure wipes them), and records the refs on the
draft. Only approved drafts export; rejected/draft_ready ones never produce files.
"""

from __future__ import annotations

import base64
import logging
import uuid

from jobpilot_core import tailor

from . import mcp_client

logger = logging.getLogger("jobpilot.cv_export")


async def run_export(user_id: uuid.UUID, draft_ids: list[str] | None = None) -> dict:
    """Render + store PDF/DOCX for approved drafts (all, or a subset of ``draft_ids``)."""
    drafts = (await mcp_client.db_mcp(user_id, "get_cv_drafts", {"status": "approved"}))["drafts"]
    if draft_ids:
        wanted = set(draft_ids)
        drafts = [d for d in drafts if d["id"] in wanted]

    exported, errors = [], {}
    for d in drafts:
        did = d["id"]
        try:
            cv = d.get("tailored_content_json") or {}
            rendered = tailor.render_both(cv)
            if not rendered["pdf_machine_readable"]:
                errors[did] = "rendered PDF is not machine-readable"
                continue
            pdf_ref = (await mcp_client.resume_mcp(user_id, "put_cv_file", {
                "draft_id": did, "fmt": "pdf",
                "file_b64": base64.b64encode(rendered["pdf"]).decode("ascii")}))["ref"]
            docx_ref = (await mcp_client.resume_mcp(user_id, "put_cv_file", {
                "draft_id": did, "fmt": "docx",
                "file_b64": base64.b64encode(rendered["docx"]).decode("ascii")}))["ref"]
            await mcp_client.db_mcp(user_id, "set_cv_draft_export", {
                "draft_id": did, "pdf_ref": pdf_ref, "docx_ref": docx_ref})
            exported.append({"draft_id": did, "pdf_ref": pdf_ref, "docx_ref": docx_ref,
                             "pdf_machine_readable": True})
        except Exception as exc:  # noqa: BLE001 - one bad draft must not sink the batch
            errors[did] = f"{type(exc).__name__}: {exc}"
            logger.warning("cv_export.draft id=%s FAILED: %s", did, exc)

    summary = {"exported": len(exported), "files": exported, "errors": errors}
    logger.info("cv_export.summary exported=%d errors=%d", len(exported), len(errors))
    return summary
