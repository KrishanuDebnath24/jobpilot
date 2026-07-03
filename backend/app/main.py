"""JobPilot FastAPI app — Phase 1.

Liveness + readiness, DB-backed auth, and the resume/jobs/erasure data paths that
run through the MCP services over HTTP (no direct ORM on those paths).
"""

from __future__ import annotations

import base64

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jobpilot_core import repositories
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
# GDPR erasure (through db-mcp)
# --------------------------------------------------------------------------- #
@app.delete("/me/data", tags=["gdpr"])
async def delete_my_data(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "delete_user_data", {})
