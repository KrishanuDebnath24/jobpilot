"""JobPilot FastAPI app — Phase 1.

Liveness + readiness, DB-backed auth, and the resume/jobs/erasure data paths that
run through the MCP services over HTTP (no direct ORM on those paths).
"""

from __future__ import annotations

import base64
import json

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from jobpilot_core import events, repositories, tracing
from jobpilot_core.resume_parse import ALLOWED_MIME
from jobpilot_core.security import create_access_token, verify_password
from jobpilot_core.settings import Settings, get_settings
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from . import mcp_client
from .auth import get_current_user
from .config import APP_NAME, VERSION
from .db import get_db
from .ratelimit import ip_rate_limit, user_rate_limit
from .readiness import check_db, check_redis

settings = get_settings()

app = FastAPI(title="JobPilot API", version=VERSION, summary="Agentic job-hunting platform.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup_observability() -> None:
    """Phase 8a: correlation-id logging + OpenTelemetry tracing (local by default).
    Idempotent; a no-op for tracing when the OTel SDK is absent or it's disabled."""
    tracing.configure_logging()
    tracing.init_tracing(settings, service_name="jobpilot-backend")


# Phase 8c: rate-limit dependencies. Limits are read live from settings (getters), so a
# config change takes effect without re-import. Triggers (paid pipeline work) are stricter
# than interactive chat; auth login is per-IP against brute force.
_rl_trigger = Depends(user_rate_limit("trigger", lambda: get_settings().rate_limit_trigger_per_min))
_rl_chat = Depends(user_rate_limit("chat", lambda: get_settings().rate_limit_chat_per_min))
_rl_auth = Depends(ip_rate_limit("auth", lambda: get_settings().rate_limit_auth_per_min))


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: str = "ok"
    service: str
    version: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: EmailStr


class ScopeRequest(BaseModel):
    """Phase 5: scope for Analyze/Enhance — selected job ids OR all qualified jobs."""
    job_ids: list[str] | None = None
    all_qualified: bool = False


class ChatMessageRequest(BaseModel):
    """Phase 6: one user turn to the Concierge."""
    text: str


class GateUpdateRequest(BaseModel):
    gate_key: str
    enabled: bool


class GateActionRequest(BaseModel):
    gate_id: str


class ApplyRequest(BaseModel):
    """Phase 6: apply accepted suggestions — only ``confirmed_skills`` get added."""
    confirmed_skills: list[str] | None = None
    label: str | None = None


class QueryRequest(BaseModel):
    """Phase 7: one read-only question to the Query agent. ``session_id`` continues an
    existing Q&A thread; omit it to start a fresh one."""
    question: str
    session_id: str | None = None


class CvTailorRequest(BaseModel):
    """Phase 6.5: tailor a CV for each selected job."""
    job_ids: list[str]


class CvActionRequest(BaseModel):
    """Phase 6.5: approve/reject drafts — a subset, or all draft_ready ones (``all``)."""
    draft_ids: list[str] | None = None
    all: bool = False


class CvIdsRequest(BaseModel):
    """Phase 6.5: export/re-qualify a subset of drafts (omit for all approved)."""
    draft_ids: list[str] | None = None


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    """Pure liveness — no dependencies."""
    return HealthResponse(service=APP_NAME, version=VERSION)


@app.get("/ready", tags=["system"])
async def ready(settings: Settings = Depends(get_settings)) -> JSONResponse:
    """Readiness — green only when DB + Redis are reachable."""
    db_ok = await check_db()
    redis_ok = await check_redis(settings)
    ready = db_ok and redis_ok
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"ready": ready, "db": db_ok, "redis": redis_ok},
    )


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@app.post("/auth/login", response_model=TokenResponse, tags=["auth"], dependencies=[_rl_auth])
async def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    user = await repositories.get_user_by_email(db, body.email)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    return TokenResponse(access_token=create_access_token(user.id, settings))


@app.get("/auth/me", response_model=UserOut, tags=["auth"])
async def read_me(current_user=Depends(get_current_user)) -> UserOut:
    return UserOut(id=str(current_user.id), email=current_user.email)


# --------------------------------------------------------------------------- #
# Resumes (through resume-mcp)
# --------------------------------------------------------------------------- #
@app.post("/resumes", tags=["resumes"])
async def upload_resume(
    file: UploadFile = File(...),
    consent: bool = Form(False),
    current_user=Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    if not consent:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="explicit consent is required to store and process your resume",
        )
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported file type {file.content_type!r}; allowed: PDF, DOCX",
        )
    data = await file.read()
    if len(data) > settings.resume_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {settings.resume_max_bytes} bytes",
        )
    result = await mcp_client.resume_mcp(
        current_user.id,
        "parse_resume",
        {
            "file_b64": base64.b64encode(data).decode("ascii"),
            "filename": file.filename or "resume",
            "declared_mime": file.content_type,
            "consent": consent,
        },
    )
    # Seed a conservative default qualifier config from the resume skills (only if the
    # user has none yet) so a sensible default is present right after upload.
    if result.get("stored"):
        skills = (result.get("structured") or {}).get("skills") or []
        try:
            await mcp_client.config_mcp(
                current_user.id, "ensure_qualifier_config", {"resume_skills": skills}
            )
        except Exception:  # noqa: BLE001 - config seeding is best-effort, never blocks upload
            pass
        # Phase 7: embed the new resume (async; never blocks the upload response).
        from .embed_runner import enqueue_embed

        enqueue_embed(current_user.id, ["resumes"])
    return result


@app.get("/resumes/me", tags=["resumes"])
async def my_resume(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.resume_mcp(current_user.id, "get_structured_resume", {})


# --------------------------------------------------------------------------- #
# Jobs (through db-mcp)
# --------------------------------------------------------------------------- #
@app.get("/jobs", tags=["jobs"])
async def list_jobs(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_jobs", {})


# --------------------------------------------------------------------------- #
# Scout (background job via Celery; run tracking through db-mcp)
# --------------------------------------------------------------------------- #
@app.post("/scout/run", tags=["scout"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def trigger_scout(current_user=Depends(get_current_user)) -> dict:
    """Queue a Scout run for the current user. Returns immediately with a task id;
    the fan-out executes in the Celery worker and records a pipeline_runs row."""
    from .tasks import scout_run

    task = scout_run.delay(str(current_user.id))
    return {"task_id": task.id, "status": "queued"}


@app.get("/scout/runs", tags=["scout"])
async def list_scout_runs(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_pipeline_runs", {"stage": "scout"})


# --------------------------------------------------------------------------- #
# Qualifier (background stage; run tracking through db-mcp)
# --------------------------------------------------------------------------- #
@app.post("/qualify/run", tags=["qualify"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def trigger_qualify(
    requalify_all: bool = False, current_user=Depends(get_current_user)
) -> dict:
    """Queue a Qualifier run for the current user (hybrid scoring over their pool)."""
    from .tasks import qualify_run

    task = qualify_run.delay(str(current_user.id), requalify_all)
    return {"task_id": task.id, "status": "queued"}


@app.get("/qualify/runs", tags=["qualify"])
async def list_qualify_runs(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_pipeline_runs", {"stage": "qualify"})


# --------------------------------------------------------------------------- #
# Pipeline orchestrator (autonomous Scout→Qualify, through pipeline-mcp)
# --------------------------------------------------------------------------- #
@app.post("/pipeline/run", tags=["pipeline"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def run_pipeline(current_user=Depends(get_current_user)) -> dict:
    """Kick off an autonomous Scout→Qualify pipeline. One active run per user; a
    second concurrent trigger is rejected with the active pipeline id."""
    return await mcp_client.pipeline_mcp(current_user.id, "run_full_pipeline", {})


@app.get("/pipeline/status", tags=["pipeline"])
async def pipeline_status(current_user=Depends(get_current_user)) -> dict:
    """Live snapshot: current stage + per-stage status/counts for the active/last run."""
    return await mcp_client.pipeline_mcp(current_user.id, "get_pipeline_status", {})


@app.post("/pipeline/stage/{stage}", tags=["pipeline"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def run_stage(stage: str, current_user=Depends(get_current_user)) -> dict:
    """Run a single stage standalone (rejected while a full pipeline is active)."""
    return await mcp_client.pipeline_mcp(current_user.id, "trigger_stage", {"stage": stage})


@app.get("/pipeline/runs", tags=["pipeline"])
async def pipeline_runs(limit: int = 20, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.pipeline_mcp(current_user.id, "list_runs", {"limit": limit})


@app.get("/pipeline/runs/{pipeline_id}", tags=["pipeline"])
async def pipeline_run_detail(pipeline_id: str, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.pipeline_mcp(current_user.id, "get_run", {"pipeline_id": pipeline_id})


@app.get("/pipeline/stream", tags=["pipeline"])
async def pipeline_stream(
    request: Request,
    current_user=Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Server-Sent Events: relay THIS user's pipeline events from Redis to the
    browser. Per-user channel only — one user's status can never leak onto another's
    stream. The client re-fetches /pipeline/status on (re)connect, then streams."""
    user_id = current_user.id

    async def gen():
        async with events.subscribe(settings.redis_url, user_id) as ps:
            yield ": connected\n\n"
            async for ev in events.listen(ps, timeout=10.0):
                if await request.is_disconnected():
                    break
                if ev is None:
                    yield ": ping\n\n"  # heartbeat keeps the connection alive
                else:
                    yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# --------------------------------------------------------------------------- #
# Dashboard (jobs joined with qualifications, through db-mcp)
# --------------------------------------------------------------------------- #
@app.get("/dashboard/jobs", tags=["dashboard"])
async def dashboard_jobs(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_qualified_jobs", {"limit": 1000})


# --------------------------------------------------------------------------- #
# Analyzer + Enhancer (Phase 5) — on-demand, over a scope (selected | all qualified)
# --------------------------------------------------------------------------- #
@app.post("/analyze/run", tags=["analyze"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def trigger_analyze(body: ScopeRequest, current_user=Depends(get_current_user)) -> dict:
    """Queue an Analyze run over the scope: structured extraction (cached sync or the
    Batch API) + deterministic aggregation. Returns a task id."""
    from .tasks import analyze_run

    task = analyze_run.delay(str(current_user.id), body.job_ids, body.all_qualified)
    return {"task_id": task.id, "status": "queued"}


@app.post("/enhance/run", tags=["enhance"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def trigger_enhance(body: ScopeRequest, current_user=Depends(get_current_user)) -> dict:
    """Queue an Enhance run: grounded, non-fabricating suggestions from the analyses."""
    from .tasks import enhance_run

    task = enhance_run.delay(str(current_user.id), body.job_ids, body.all_qualified)
    return {"task_id": task.id, "status": "queued"}


@app.get("/analyze/runs", tags=["analyze"])
async def list_analyze_runs(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_pipeline_runs", {"stage": "analyze"})


@app.get("/enhance/runs", tags=["enhance"])
async def list_enhance_runs(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_pipeline_runs", {"stage": "enhance"})


@app.get("/analyses", tags=["analyze"])
async def list_analyses(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_analyses", {})


@app.get("/suggestions", tags=["enhance"])
async def list_suggestions(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_suggestions", {})


@app.post("/suggestions/{suggestion_id}/status", tags=["enhance"])
async def set_suggestion_status(
    suggestion_id: str, status: str, current_user=Depends(get_current_user)
) -> dict:
    """Accept/dismiss a suggestion (lifecycle: proposed → accepted | dismissed)."""
    if status not in ("proposed", "accepted", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be proposed|accepted|dismissed")
    return await mcp_client.db_mcp(
        current_user.id, "update_suggestion_status",
        {"suggestion_id": suggestion_id, "status": status})


# --------------------------------------------------------------------------- #
# Concierge chat (Phase 6) — the acting agent, scoped through MCP
# --------------------------------------------------------------------------- #
@app.post("/chat/sessions", tags=["chat"])
async def create_chat_session(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "create_chat_session", {})


@app.get("/chat/sessions", tags=["chat"])
async def list_chat_sessions(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "list_chat_sessions", {})


@app.get("/chat/sessions/{session_id}/messages", tags=["chat"])
async def chat_messages(session_id: str, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(
        current_user.id, "get_messages", {"chat_session_id": session_id})


@app.post("/chat/sessions/{session_id}/message", tags=["chat"], dependencies=[_rl_chat])
async def post_chat_message(
    session_id: str, body: ChatMessageRequest, current_user=Depends(get_current_user)
) -> dict:
    """Run one Concierge turn. The agent acts only through user-scoped MCP tools;
    consequential/paid actions are confirmed in-conversation before firing."""
    from .concierge_runner import run_turn
    import uuid as _uuid

    try:
        return await run_turn(current_user.id, _uuid.UUID(session_id), body.text)
    except RuntimeError as exc:  # e.g. missing Anthropic key
        raise HTTPException(status_code=503, detail=str(exc))


# --------------------------------------------------------------------------- #
# Gates (Phase 6) — human-in-the-loop review config + approve/skip
# --------------------------------------------------------------------------- #
@app.get("/gates", tags=["gates"])
async def get_gates(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.config_mcp(current_user.id, "get_gate_config", {})


@app.post("/gates", tags=["gates"])
async def update_gate(body: GateUpdateRequest, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.config_mcp(
        current_user.id, "update_gate_config",
        {"gate_key": body.gate_key, "enabled": body.enabled})


@app.post("/pipeline/gate/approve", tags=["gates"])
async def approve_gate(body: GateActionRequest, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.pipeline_mcp(current_user.id, "approve_gate", {"gate_id": body.gate_id})


@app.post("/pipeline/gate/skip", tags=["gates"])
async def skip_gate(body: GateActionRequest, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.pipeline_mcp(current_user.id, "skip_gate", {"gate_id": body.gate_id})


# --------------------------------------------------------------------------- #
# Apply-to-resume (Phase 6) — gated, versioned, honest
# --------------------------------------------------------------------------- #
@app.post("/resume/apply/prepare", tags=["resume"])
async def prepare_resume_apply(
    body: ApplyRequest, current_user=Depends(get_current_user)
) -> dict:
    """Preview the new resume version (the approval gate) — no changes are written."""
    from .apply_runner import prepare_apply

    return await prepare_apply(current_user.id, body.confirmed_skills)


@app.post("/resume/apply", tags=["resume"])
async def apply_resume(body: ApplyRequest, current_user=Depends(get_current_user)) -> dict:
    """Write a new resume version from accepted suggestions. Only confirmed skills are
    added; the original is preserved."""
    from .apply_runner import apply_to_resume

    return await apply_to_resume(current_user.id, body.confirmed_skills, body.label)


@app.get("/resume/versions", tags=["resume"])
async def resume_versions(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.resume_mcp(current_user.id, "list_resume_versions", {})


@app.get("/resume/versions/{resume_id}", tags=["resume"])
async def resume_version(resume_id: str, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.resume_mcp(
        current_user.id, "get_resume_version", {"resume_id": resume_id})


@app.get("/resume/versions/{resume_id}/download", tags=["resume"])
async def download_resume_version(resume_id: str, current_user=Depends(get_current_user)):
    """Download a resume version as plain text."""
    from fastapi.responses import PlainTextResponse

    res = await mcp_client.resume_mcp(
        current_user.id, "get_resume_version", {"resume_id": resume_id})
    resume = res.get("resume")
    if not resume:
        raise HTTPException(status_code=404, detail="resume version not found")
    return PlainTextResponse(
        resume.get("raw_text") or "",
        headers={"Content-Disposition": f'attachment; filename="resume-{resume_id[:8]}.txt"'},
    )


# --------------------------------------------------------------------------- #
# Tailored CVs (Phase 6.5) — tailor → review/approve → export → re-qualify
# --------------------------------------------------------------------------- #
async def _resolve_draft_ids(user_id, body: CvActionRequest) -> list[str]:
    """Approve/reject targets: the given ids, or every draft_ready draft when ``all``."""
    if body.draft_ids:
        return body.draft_ids
    if body.all:
        drafts = (await mcp_client.db_mcp(user_id, "get_cv_drafts", {"status": "draft_ready"}))["drafts"]
        return [d["id"] for d in drafts]
    return []


@app.post("/cv/tailor", tags=["cv"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def cv_tailor(body: CvTailorRequest, current_user=Depends(get_current_user)) -> dict:
    """Queue tailoring for the selected jobs (Tailor + Critic, locked-safe). Only the
    user's selected jobs are tailored — never the whole pool."""
    from .tasks import cv_tailor as task

    t = task.delay(str(current_user.id), body.job_ids)
    return {"task_id": t.id, "status": "queued", "job_count": len(body.job_ids)}


@app.get("/cv/drafts", tags=["cv"])
async def cv_drafts(status: str | None = None, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_cv_drafts", {"status": status})


@app.get("/cv/drafts/{draft_id}", tags=["cv"])
async def cv_draft(draft_id: str, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_cv_draft", {"draft_id": draft_id})


@app.post("/cv/approve", tags=["cv"])
async def cv_approve(body: CvActionRequest, current_user=Depends(get_current_user)) -> dict:
    ids = await _resolve_draft_ids(current_user.id, body)
    return await mcp_client.db_mcp(
        current_user.id, "set_cv_draft_status", {"draft_ids": ids, "status": "approved"})


@app.post("/cv/reject", tags=["cv"])
async def cv_reject(body: CvActionRequest, current_user=Depends(get_current_user)) -> dict:
    ids = await _resolve_draft_ids(current_user.id, body)
    return await mcp_client.db_mcp(
        current_user.id, "set_cv_draft_status", {"draft_ids": ids, "status": "rejected"})


@app.post("/cv/export", tags=["cv"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def cv_export(body: CvIdsRequest, current_user=Depends(get_current_user)) -> dict:
    """Render approved drafts to PDF+DOCX (Irish/ATS-safe). Only approved drafts export."""
    from .tasks import cv_export as task

    t = task.delay(str(current_user.id), body.draft_ids)
    return {"task_id": t.id, "status": "queued"}


@app.post("/cv/requalify", tags=["cv"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def cv_requalify(body: CvIdsRequest, current_user=Depends(get_current_user)) -> dict:
    """Re-qualify tailored drafts against their jobs (batched); records after_score."""
    from .tasks import cv_requalify as task

    t = task.delay(str(current_user.id), body.draft_ids)
    return {"task_id": t.id, "status": "queued"}


@app.get("/cv/report", tags=["cv"])
async def cv_report(current_user=Depends(get_current_user)) -> dict:
    """Improvement report: before/after scores + deltas for drafts that were re-qualified.
    Read-only summary for the UI/Concierge (grounded in the drafts' recorded scores)."""
    drafts = (await mcp_client.db_mcp(current_user.id, "get_cv_drafts", {}))["drafts"]
    jobs = (await mcp_client.db_mcp(current_user.id, "get_qualified_jobs", {"limit": 5000}))["jobs"]
    by_id = {j["id"]: j for j in jobs}
    rows, improved, unchanged, worse = [], 0, 0, 0
    for d in drafts:
        if d.get("after_score") is None:
            continue
        before, after = d.get("before_score"), d["after_score"]
        delta = round(after - before, 4) if before is not None else None
        if delta is not None:
            improved += delta > 0.0001
            worse += delta < -0.0001
            unchanged += abs(delta) <= 0.0001
        job = by_id.get(d["job_id"]) or {}
        rows.append({"draft_id": d["id"], "job_id": d["job_id"], "job_title": job.get("title"),
                     "company": job.get("company"), "before_score": before, "after_score": after,
                     "delta": delta, "status": d["status"]})
    rows.sort(key=lambda r: (r["delta"] if r["delta"] is not None else -1), reverse=True)
    return {"report": rows, "improved": improved, "unchanged": unchanged, "worse": worse,
            "total": len(rows)}


@app.get("/cv/drafts/{draft_id}/download/{fmt}", tags=["cv"])
async def cv_download(draft_id: str, fmt: str, current_user=Depends(get_current_user)):
    """Download a rendered tailored CV (fmt = pdf|docx)."""
    from fastapi.responses import Response

    if fmt not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="fmt must be pdf|docx")
    res = await mcp_client.db_mcp(current_user.id, "get_cv_draft", {"draft_id": draft_id})
    draft = res.get("draft")
    if not draft:
        raise HTTPException(status_code=404, detail="draft not found")
    ref = draft.get("pdf_ref") if fmt == "pdf" else draft.get("docx_ref")
    if not ref:
        raise HTTPException(status_code=404, detail="not exported yet — export first")
    fetched = await mcp_client.resume_mcp(current_user.id, "get_cv_file", {"ref": ref})
    if fetched.get("error") or not fetched.get("file_b64"):
        raise HTTPException(status_code=404, detail="file not found")
    data = base64.b64decode(fetched["file_b64"])
    media = "application/pdf" if fmt == "pdf" else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return Response(
        content=data, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="cv-{draft_id[:8]}.{fmt}"'})


# --------------------------------------------------------------------------- #
# Query agent (Phase 7) — read-only Q&A over the user's data + work status
# --------------------------------------------------------------------------- #
@app.post("/query/session", tags=["query"])
async def create_query_session(current_user=Depends(get_current_user)) -> dict:
    """Start a Query (Ask-about-your-data) thread. Its own session, distinct from the
    Concierge's, so the read-only agent and the acting agent never share a transcript."""
    return await mcp_client.db_mcp(current_user.id, "create_chat_session", {"title": "query"})


@app.get("/query/session/{session_id}/messages", tags=["query"])
async def query_messages(session_id: str, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(
        current_user.id, "get_messages", {"chat_session_id": session_id})


@app.post("/query/ask", tags=["query"], dependencies=[_rl_chat])
async def query_ask(body: QueryRequest, current_user=Depends(get_current_user)) -> dict:
    """Answer one read-only question — routed to exact/semantic/status tools, grounded
    and cited. Creates a session if none is supplied. The Query agent has NO action
    tools; action requests are redirected to the Concierge."""
    import uuid as _uuid

    from .query_runner import run_query_turn

    session_id = body.session_id
    if not session_id:
        session_id = (await mcp_client.db_mcp(
            current_user.id, "create_chat_session", {"title": "query"}))["id"]
    try:
        turn = await run_query_turn(current_user.id, _uuid.UUID(session_id), body.question)
    except RuntimeError as exc:  # e.g. missing Anthropic key
        raise HTTPException(status_code=503, detail=str(exc))
    return {**turn, "session_id": session_id}


# --------------------------------------------------------------------------- #
# Embeddings (Phase 7) — backfill + coverage. Population is async in the worker.
# --------------------------------------------------------------------------- #
@app.post("/embeddings/backfill", tags=["embeddings"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def backfill_embeddings(current_user=Depends(get_current_user)) -> dict:
    """Queue an embedding backfill for the current user (all kinds). Idempotent — only
    rows without an embedding are touched."""
    from .tasks import embed_run

    task = embed_run.delay(str(current_user.id), None)
    return {"task_id": task.id, "status": "queued"}


@app.get("/embeddings/coverage", tags=["embeddings"])
async def embeddings_coverage(current_user=Depends(get_current_user)) -> dict:
    """Per-kind {total, embedded} — how much of the pool is semantically searchable."""
    return await mcp_client.db_mcp(current_user.id, "embedding_coverage", {})


# --------------------------------------------------------------------------- #
# Cost report (Phase 8b) — per-user LLM spend from the logged run usage
# --------------------------------------------------------------------------- #
@app.get("/cost/report", tags=["cost"])
async def cost_report(limit: int = 500, current_user=Depends(get_current_user)) -> dict:
    """Per-user LLM cost: total $, breakdown by agent + by day, token totals, the
    batch/cache-discount confirmation, and a soft daily-threshold alert. Built from the
    ``llm_usage`` the runners log onto ``pipeline_runs`` (estimates; billing is
    authoritative). Read-only, user-scoped via the signed MCP context."""
    from jobpilot_core import cost

    runs = (await mcp_client.db_mcp(current_user.id, "get_pipeline_runs", {"limit": limit})).get("runs", [])
    report = cost.aggregate_cost(runs, daily_threshold_usd=settings.cost_alert_daily_usd)
    if report["over_threshold"]:  # the alert — fires on a threshold breach (no PII, ids/$ only)
        import logging

        logging.getLogger("jobpilot.cost").warning(
            "cost.alert user over daily threshold days=%s threshold_usd=%s",
            [a["date"] for a in report["alerts"]], settings.cost_alert_daily_usd)
    return report


# --------------------------------------------------------------------------- #
# GDPR erasure (through db-mcp)
# --------------------------------------------------------------------------- #
@app.delete("/me/data", tags=["gdpr"])
async def delete_my_data(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "delete_user_data", {})
