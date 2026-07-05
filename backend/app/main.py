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
from jobpilot_core import events, repositories
from jobpilot_core.resume_parse import ALLOWED_MIME
from jobpilot_core.security import create_access_token, verify_password
from jobpilot_core.settings import Settings, get_settings
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from . import mcp_client
from .auth import get_current_user
from .config import APP_NAME, VERSION
from .db import get_db
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
@app.post("/auth/login", response_model=TokenResponse, tags=["auth"])
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
@app.post("/scout/run", tags=["scout"], status_code=status.HTTP_202_ACCEPTED)
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
@app.post("/qualify/run", tags=["qualify"], status_code=status.HTTP_202_ACCEPTED)
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
@app.post("/pipeline/run", tags=["pipeline"], status_code=status.HTTP_202_ACCEPTED)
async def run_pipeline(current_user=Depends(get_current_user)) -> dict:
    """Kick off an autonomous Scout→Qualify pipeline. One active run per user; a
    second concurrent trigger is rejected with the active pipeline id."""
    return await mcp_client.pipeline_mcp(current_user.id, "run_full_pipeline", {})


@app.get("/pipeline/status", tags=["pipeline"])
async def pipeline_status(current_user=Depends(get_current_user)) -> dict:
    """Live snapshot: current stage + per-stage status/counts for the active/last run."""
    return await mcp_client.pipeline_mcp(current_user.id, "get_pipeline_status", {})


@app.post("/pipeline/stage/{stage}", tags=["pipeline"], status_code=status.HTTP_202_ACCEPTED)
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
# GDPR erasure (through db-mcp)
# --------------------------------------------------------------------------- #
@app.delete("/me/data", tags=["gdpr"])
async def delete_my_data(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "delete_user_data", {})
