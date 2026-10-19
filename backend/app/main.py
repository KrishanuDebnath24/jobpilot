"""JobPilot FastAPI app — Phase 1.

Liveness + readiness, DB-backed auth, and the resume/jobs/erasure data paths that
run through the MCP services over HTTP (no direct ORM on those paths).
"""

from __future__ import annotations

import base64
import json
import logging
import re

from anthropic import APIError, APIStatusError, AuthenticationError, RateLimitError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from jobpilot_core import events, repositories, tracing
from jobpilot_core.resume_parse import ALLOWED_MIME
from jobpilot_core.security import create_access_token, hash_password, verify_password
from jobpilot_core.settings import Settings, get_settings
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from . import mcp_client
from .auth import get_current_user
from .config import APP_NAME, VERSION
from .db import get_db
from .ratelimit import ip_rate_limit, user_rate_limit
from .readiness import check_db, check_redis

settings = get_settings()
logger = logging.getLogger("jobpilot.api")

app = FastAPI(title="JobPilot API", version=VERSION, summary="Agentic job-hunting platform.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Anthropic failures → a clean, CORS-carrying response.
#
# The interactive LLM endpoints (Query agent, Concierge chat) call Anthropic inside the
# request. A raised `anthropic.APIError` — out of credits, rate-limited, bad key, upstream
# outage — would otherwise escape as an UNHANDLED 500, and an unhandled 500 is produced by
# the outermost ServerErrorMiddleware *above* the CORS middleware, so it carries no
# `access-control-allow-origin` header. The browser then blocks the headerless response and
# reports a bare "TypeError: NetworkError", hiding the real cause. A *registered* handler
# runs inside the CORS middleware, so its response gets the CORS headers, the browser reads
# it, and the UI shows the real message. (Async worker LLM calls — analyze/enhance/cv — fail
# in Celery, not here, and are reported through their run status; this covers the request
# path only.)
# --------------------------------------------------------------------------- #
def _anthropic_error_response(exc: APIError) -> tuple[int, str]:
    """Map an Anthropic failure to (http_status, user-facing detail). Rate limits are 429
    (retryable); every other provider-side condition is 503 (the AI service is unavailable
    to us — never a 4xx that would wrongly blame the caller's request)."""
    if isinstance(exc, RateLimitError):
        return 429, "The AI service is rate-limited right now — please try again in a moment."
    # Pull the provider's own message out of the error body when present.
    provider_msg = ""
    if isinstance(exc, APIStatusError):
        body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
        provider_msg = ((body.get("error") or {}).get("message")) or ""
    text = (provider_msg or str(exc)).lower()
    if "credit balance is too low" in text:
        return 503, (
            "The AI service is unavailable: the Anthropic account is out of credits. "
            "Add credits at console.anthropic.com → Plans & Billing, then try again."
        )
    if isinstance(exc, AuthenticationError):
        return 503, "The AI service rejected the API key (check ANTHROPIC_API_KEY)."
    return 503, "The AI service is temporarily unavailable — please try again shortly."


@app.exception_handler(APIError)
async def _handle_anthropic_error(request: Request, exc: APIError) -> JSONResponse:
    status_code, detail = _anthropic_error_response(exc)
    logger.warning("anthropic.error path=%s type=%s: %s",
                   request.url.path, type(exc).__name__, exc)
    return JSONResponse(status_code=status_code, content={"detail": detail})


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


class RegisterRequest(BaseModel):
    """Self-serve sign-up. Password is length-checked here; email uniqueness is enforced
    at the route (409 on conflict)."""
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


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
    """Phase 6: one user turn to the Concierge. ``selected_job_ids`` is the user's current
    dashboard selection, so 'tailor/analyze/delete my selected jobs' resolves to it."""
    text: str
    selected_job_ids: list[str] | None = None


class GateUpdateRequest(BaseModel):
    gate_key: str
    enabled: bool


class GateActionRequest(BaseModel):
    gate_id: str


class SearchConfigUpdate(BaseModel):
    """Partial update of the user's search config — only provided fields are written."""
    roles: list[str] | None = None
    locations: list[str] | None = None
    keywords: list[str] | None = None
    sources: list[str] | None = None
    target_employers: list[str] | None = None
    schedule: str | None = None


class QualifierConfigUpdate(BaseModel):
    """Partial update of the user's qualifier config — only provided fields are written.
    ``must_have`` is the hard gate: a job missing any must-have term is disqualified (so
    e.g. ``["contract"]`` narrows the qualified set to contract postings)."""
    weights_json: dict | None = None
    threshold: float | None = None
    must_have: list[str] | None = None
    nice_to_have: list[str] | None = None


class ApplicationProfileUpdate(BaseModel):
    """Partial update of the pre-configured application profile.

    Every field is optional and defaults are absent rather than ``None``, so the endpoint
    can use ``exclude_unset`` to tell "leave this alone" apart from "clear this" — the
    difference matters for ``willing_to_relocate``, where ``False`` is a real answer.

    ``eligibility_json`` is keyed by jurisdiction (``{"IE": {"authorized": true, …}}``)
    because work authorisation differs per country while the question wording barely
    does; ``demographics_json`` holds voluntary EEO answers, defaulting to
    "prefer not to say".
    """

    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address_line1: str | None = None
    city: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    notice_period_days: int | None = None
    earliest_start_date: str | None = None  # ISO-8601 date
    salary_expectation: int | None = None
    salary_currency: str | None = None
    willing_to_relocate: bool | None = None
    work_mode_preference: str | None = None
    eligibility_json: dict | None = None
    demographics_json: dict | None = None


class AnswerUpsert(BaseModel):
    """Store or re-confirm one screening answer.

    Note there is no ``locked`` field: whether a question is legally sensitive is decided
    server-side from its content, so a client cannot mark a work-eligibility question as
    ordinary and opt it into similarity matching.
    """

    question_text: str
    answer_value: str
    answer_kind: str = "text"
    scope: str = "global"


class ResolveApplyRequest(BaseModel):
    """Batch size and whether to re-resolve rows that already have an answer.

    ``force`` is for after new ATS patterns are added — previously "unknown" listings
    deserve a second look, but a plain run must not re-hit employers' servers for jobs
    already resolved.
    """

    limit: int = 200
    force: bool = False


class FormField(BaseModel):
    """One field scraped off a live application form by the browser extension.

    ``ref`` is the extension's own handle for the element and is echoed back untouched —
    the server never sees a selector and has no way to address the page, which keeps the
    autofill strictly a request the page makes rather than something done to it.
    """

    ref: str
    label: str | None = None
    aria_label: str | None = None
    placeholder: str | None = None
    name: str | None = None
    type: str = "text"
    autocomplete: str | None = None
    required: bool = False
    options: list[dict] | None = None


class ApplyPlanRequest(BaseModel):
    """Ask for the answers that belong on this form.

    ``job_id`` is optional: the extension can autofill a form the user navigated to on
    their own, and without a job the plan simply has no employer scope and no CV to
    attach. Everything else works the same.
    """

    fields: list[FormField]
    job_id: str | None = None
    url: str | None = None


class SubmittedAnswer(BaseModel):
    """One field as it stood when the user pressed the form's own Submit button.

    ``edited`` is the whole point of the shape: a value that arrived from the bank and
    went in unchanged is a reuse, while one the user retyped is a correction that should
    overwrite what was stored.
    """

    question: str
    value: str | None = None
    kind: str = "text"
    tier: str | None = None
    source_id: str | None = None
    edited: bool = False


class SubmittedCv(BaseModel):
    """Which CV the extension actually put on the form.

    Reported by the content script rather than re-derived server-side because only the
    frame that did it knows whether the file landed: the input may already have carried
    the user's own upload, the form may have had no CV field at all, or the fetch may
    have failed. ``attached=False`` is a real answer, not a missing one.
    """

    attached: bool = False
    source: str | None = None      # tailored | original
    draft_id: str | None = None    # set when source == tailored


class ApplyRecordRequest(BaseModel):
    """What was submitted — for the bank to learn from, and for the application log."""

    answers: list[SubmittedAnswer]
    job_id: str | None = None
    cv: SubmittedCv | None = None


class DeriveConfigRequest(BaseModel):
    """Ask the LLM to propose a market-canonical config from the resume (review-only, no
    write). ``hint`` is the optional free-text steer typed in the review dialog."""
    hint: str | None = None


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


class CvDraftDeleteRequest(BaseModel):
    """Delete tailored-CV drafts outright. Ids are required and there is no ``all`` flag:
    unlike approve/reject this cannot be undone, so it never happens by omission."""
    draft_ids: list[str]


class GapCloseItemPatch(BaseModel):
    """Phase 6.6: one item of the user's review of a gap-close proposal set. ``accepted``
    None leaves it undecided; the text fields let the user edit the wording before
    vouching for it. Unknown kinds/ids are ignored server-side."""
    kind: str
    id: str
    accepted: bool | None = None
    skill: str | None = None
    text: str | None = None
    after: str | None = None
    role_ref: str | None = None
    interview_prep: str | None = None


class GapCloseReviewRequest(BaseModel):
    items: list[GapCloseItemPatch]


class GapCloseDeleteRequest(BaseModel):
    """Phase 6.6: discard whole proposal sets. Identified by suggestion id (what the review
    endpoint uses), and only the caller's own rows can be touched."""
    suggestion_ids: list[str]


class CvDraftEditRequest(BaseModel):
    """Phase 6.6: deterministic edits to one attached draft (no LLM). Adding a skill the
    résumé doesn't evidence is recorded as ``user_confirmed`` — the candidate's own word."""
    add_skills: list[str] | None = None
    remove_skills: list[str] | None = None
    add_bullets: list[dict] | None = None     # [{"text", "role_ref"?, "role_index"?}]
    remove_bullets: list[str] | None = None


class CvDraftRewordRequest(BaseModel):
    """Phase 6.6: one focused model call rewording the summary or a single role's bullets."""
    target: str = "summary"                   # "summary" | "role"
    instruction: str
    role_index: int | None = None
    role_ref: str | None = None


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


@app.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED,
          tags=["auth"], dependencies=[_rl_auth])
async def register(
    body: RegisterRequest,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Create an account and sign in immediately. Rejects a duplicate email with 409;
    rate-limited per-IP like login. Email match is case-insensitive."""
    if await repositories.get_user_by_email(db, body.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )
    user = await repositories.create_user(db, body.email, hash_password(body.password))
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


@app.post("/resumes/me/reparse", tags=["resumes"], dependencies=[_rl_trigger])
async def reparse_resume(current_user=Depends(get_current_user)) -> dict:
    """Re-read the CV already on file with the current parser.

    Use it when the extractor has learned a field the stored parse predates — a role's
    location, or a section it had nowhere to put — so an existing resume gains it without
    a re-upload. The row keeps its id, so tailored CVs stay attached to their jobs; they
    still need re-tailoring to pick the new content up. Costs one model call."""
    result = await mcp_client.resume_mcp(current_user.id, "reparse_resume", {})
    if result.get("reparsed"):
        # The stored text changed, so its embedding is now stale (same as after an upload).
        from .embed_runner import enqueue_embed

        enqueue_embed(current_user.id, ["resumes"])
    return result


@app.get("/resumes/me", tags=["resumes"])
async def my_resume(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.resume_mcp(current_user.id, "get_structured_resume", {})


@app.get("/resumes/me/base", tags=["resumes"])
async def my_base_resume(current_user=Depends(get_current_user)) -> dict:
    """The WORKING resume (latest uploaded) that scoring/tailoring use. Assembled
    apply-to-resume versions never appear here — so they can't take over qualification."""
    return await mcp_client.resume_mcp(current_user.id, "get_base_resume", {})


def _resume_media_type(ref: str) -> tuple[str, str]:
    """(media_type, ext) from a stored file_ref's extension. mime isn't a column; the
    key carries the extension (see storage.resume_key)."""
    ext = ref.rsplit(".", 1)[-1].lower() if "." in ref else "pdf"
    if ext == "pdf":
        return "application/pdf", "pdf"
    if ext == "docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
    return "application/octet-stream", ext


@app.get("/resumes/me/file", tags=["resumes"])
async def download_my_resume_file(current_user=Depends(get_current_user)):
    """Stream the caller's most recent *uploaded* resume as its original file (PDF/DOCX),
    inline so the browser can preview it. 404 if the latest resume is assembled/file-less."""
    from fastapi.responses import Response

    res = await mcp_client.resume_mcp(current_user.id, "get_resume_file", {})
    if res.get("error") or not res.get("file_b64"):
        raise HTTPException(status_code=404, detail=res.get("error") or "no uploaded resume file")
    data = base64.b64decode(res["file_b64"])
    media, ext = _resume_media_type(res.get("file_ref") or "")
    return Response(
        content=data, media_type=media,
        headers={"Content-Disposition": f'inline; filename="resume.{ext}"'})


# --------------------------------------------------------------------------- #
# Jobs (through db-mcp)
# --------------------------------------------------------------------------- #
@app.get("/jobs", tags=["jobs"])
async def list_jobs(
    employment_type: str | None = None,
    seniority: str | None = None,
    current_user=Depends(get_current_user),
) -> dict:
    """List the user's jobs, optionally narrowed by employment type (e.g. ``contract``)
    or seniority (e.g. ``junior``/``intern`` for freshers). Both are deterministically
    classified at scout time; a NULL value never matches a filter."""
    args = {k: v for k, v in {"employment_type": employment_type, "seniority": seniority}.items() if v}
    return await mcp_client.db_mcp(current_user.id, "get_jobs", args)


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
# Search config — what Scout fans out on. Read/updated by the user in the UI;
# an LLM proposes a market-canonical config from the resume (propose-then-confirm).
# --------------------------------------------------------------------------- #
@app.get("/search-config", tags=["search-config"])
async def get_search_config(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.config_mcp(current_user.id, "get_search_config", {})


@app.put("/search-config", tags=["search-config"])
async def update_search_config(
    body: SearchConfigUpdate, current_user=Depends(get_current_user)
) -> dict:
    # Only send keys the caller actually set, so a partial update never clears a field.
    fields = body.model_dump(exclude_none=True)
    return await mcp_client.config_mcp(current_user.id, "update_search_config", fields)


@app.post("/search-config/derive", tags=["search-config"], dependencies=[_rl_trigger])
async def derive_search_config(
    body: DeriveConfigRequest, current_user=Depends(get_current_user)
) -> dict:
    """Propose a market-canonical config from the caller's resume (review-only, no write).
    The frontend pre-fills the review dialog with this; Save calls PUT /search-config."""
    from .config_deriver_runner import propose_config

    try:
        return await propose_config(current_user.id, body.hint)
    except RuntimeError as exc:  # e.g. missing Anthropic key surfaced by the LLM path
        raise HTTPException(status_code=503, detail=str(exc))


# --------------------------------------------------------------------------- #
# Qualifier config — the scored weights/threshold + the hard must-have gate. Read/
# updated by the user; the gate is the lever to narrow the qualified set by a term
# (e.g. must_have=["contract"]). Proxied to config-mcp (user-scoped by the signed ctx).
# --------------------------------------------------------------------------- #
@app.get("/qualifier-config", tags=["qualifier-config"])
async def get_qualifier_config(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.config_mcp(current_user.id, "get_qualifier_config", {})


@app.put("/qualifier-config", tags=["qualifier-config"])
async def update_qualifier_config(
    body: QualifierConfigUpdate, current_user=Depends(get_current_user)
) -> dict:
    # Partial update: only send keys the caller actually set, so an omitted field is left
    # untouched rather than cleared.
    fields = body.model_dump(exclude_none=True)
    return await mcp_client.config_mcp(current_user.id, "update_qualifier_config", fields)


# --------------------------------------------------------------------------- #
# Application profile + answer bank (apply agent, stage 1)
#
# The pre-configured facts an apply agent may submit on the user's behalf, and the
# questions they have already answered. Read/write only — nothing here touches a browser
# or an LLM, and no value on either surface is ever model-generated.
# --------------------------------------------------------------------------- #
@app.get("/application-profile", tags=["apply"])
async def get_application_profile(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.config_mcp(current_user.id, "get_application_profile", {})


@app.put("/application-profile", tags=["apply"])
async def update_application_profile(
    body: ApplicationProfileUpdate, current_user=Depends(get_current_user)
) -> dict:
    # ``exclude_unset`` (not ``exclude_none``): an explicit null is a deliberate clear,
    # which must be distinguishable from a field the client simply didn't send.
    fields = body.model_dump(exclude_unset=True)
    return await mcp_client.config_mcp(current_user.id, "update_application_profile", {"fields": fields})


@app.post("/application-profile/seed", tags=["apply"])
async def seed_application_profile(current_user=Depends(get_current_user)) -> dict:
    """Fill blank profile fields from the parsed résumé. Never overwrites a set value."""
    return await mcp_client.config_mcp(current_user.id, "seed_application_profile", {})


@app.get("/apply/coverage", tags=["apply"])
async def get_apply_coverage(current_user=Depends(get_current_user)) -> dict:
    """How many jobs have a reachable application form, and through which ATS."""
    return await mcp_client.db_mcp(current_user.id, "get_apply_coverage", {})


@app.post("/apply/resolve", status_code=status.HTTP_202_ACCEPTED, tags=["apply"])
async def resolve_apply_targets(
    body: ResolveApplyRequest, current_user=Depends(get_current_user)
) -> dict:
    """Queue redirect resolution for jobs whose apply target is unknown or stale.

    Returns 202 the moment the task is queued — resolving hundreds of listings is
    deliberately paced, so the client polls ``/apply/coverage`` for progress rather than
    waiting. Only follows links; nothing is submitted anywhere.
    """
    from .tasks import apply_resolve

    task = apply_resolve.delay(str(current_user.id), body.limit, body.force)
    return {"queued": True, "task_id": task.id, "limit": body.limit, "force": body.force}


async def _apply_context(user_id, job_id: str | None) -> dict:
    """Employer identity and the CV to attach, for one job.

    Split out because both autofill routes need the same employer scope, and they must
    agree on it: a plan resolved against ``employer:stripe`` whose answers were banked
    against ``employer:stripe-payments`` would learn nothing reusable.

    A job that cannot be read (deleted, or another user's) degrades to no scope rather
    than erroring — the extension can still fill a form from the global bank, which is
    strictly better than refusing to work.
    """
    if not job_id:
        return {"job": None, "employer_slug": None, "company": None, "cv": None}
    try:
        job = (await mcp_client.db_mcp(user_id, "get_job", {"job_id": job_id})).get("job")
    except Exception:  # noqa: BLE001 - see docstring: no job is a degraded mode, not a failure
        job = None
    if not job:
        return {"job": None, "employer_slug": None, "company": None, "cv": None}

    # The tailored CV for this job when one has been exported, else the uploaded original.
    # Only an exported draft counts: an un-exported draft has no rendered file behind it.
    cv = {"path": "/resumes/me/file", "tailored": False, "draft_id": None}
    try:
        drafts = (await mcp_client.db_mcp(user_id, "get_cv_drafts", {})).get("drafts") or []
        match = next(
            (d for d in drafts if d.get("job_id") == job_id and d.get("pdf_ref")), None
        )
        if match:
            # ``draft_id`` rides along so /apply/record can say WHICH tailored CV was sent
            # without re-resolving. There is at most one draft per job
            # (uq_cv_draft_per_user_job), so this is the only candidate the job ever has.
            cv = {
                "path": f"/cv/drafts/{match['id']}/download/pdf",
                "tailored": True,
                "draft_id": match["id"],
            }
    except Exception:  # noqa: BLE001 - a missing tailored CV falls back to the base one
        pass

    return {
        "job": job,
        "employer_slug": job.get("apply_board_slug"),
        "company": job.get("company"),
        "cv": cv,
    }


@app.post("/apply/plan", tags=["apply"])
async def plan_application(
    body: ApplyPlanRequest, current_user=Depends(get_current_user)
) -> dict:
    """Answer a scraped application form from the profile and the answer bank.

    Read-only, and idempotent by construction: it resolves fields and returns values with
    their provenance. Nothing is filled by this call and nothing is submitted anywhere —
    the extension does the typing, in the user's own browser, with the form on screen.
    """
    ctx = await _apply_context(current_user.id, body.job_id)
    plan = await mcp_client.config_mcp(
        current_user.id,
        "plan_application",
        {
            "fields": [f.model_dump() for f in body.fields],
            "employer_slug": ctx["employer_slug"],
            "company": ctx["company"],
        },
    )
    if ctx["cv"] and plan.get("summary", {}).get("attachments"):
        name = (plan.get("applicant_name") or "").strip()
        stem = re.sub(r"[^\w-]+", "-", name).strip("-") or "CV"
        plan["cv"] = {**ctx["cv"], "filename": f"{stem}-CV.pdf" if name else "CV.pdf"}
    plan["job"] = (
        {
            "id": ctx["job"]["id"],
            "title": ctx["job"].get("title"),
            "company": ctx["job"].get("company"),
            "apply_ats": ctx["job"].get("apply_ats"),
        }
        if ctx["job"]
        else None
    )
    return plan


def _recorded_cv(ctx: dict, reported: SubmittedCv | None) -> dict:
    """Which CV to log against this application.

    The extension's report is authoritative on *whether* a file was attached — it is the
    only party that watched it happen. The draft id is then reconciled against the job,
    which is unambiguous because a job has at most one draft
    (``uq_cv_draft_per_user_job``): if the job still has one, that is necessarily the one
    that was sent. A draft deleted between filling and submitting leaves ``cv_source``
    honest at ``tailored`` with a null id, rather than silently demoting the record to
    "original" and misreporting what the employer received.
    """
    if reported is None or not reported.attached:
        return {"cv_source": "none", "cv_draft_id": None, "cv_label": None}
    if reported.source == "tailored":
        draft_id = (ctx.get("cv") or {}).get("draft_id")
        return {
            "cv_source": "tailored",
            "cv_draft_id": draft_id,
            "cv_label": "Tailored CV" if draft_id else "Tailored CV (draft since deleted)",
        }
    return {"cv_source": "original", "cv_draft_id": None, "cv_label": "Original resume"}


@app.post("/apply/record", tags=["apply"])
async def record_application(
    body: ApplyRecordRequest, current_user=Depends(get_current_user)
) -> dict:
    """Learn from a form the user just submitted, and log that they submitted it.

    Every answer stored here was on screen, in a form the user chose to submit — which is
    the standard the never-infer rule asks for. Values that came from the profile or the
    bank unchanged are counted as reuse instead of rewritten, so the bank records what was
    actually re-authored.

    The application log holds itself to the same standard. A row is written only when the
    extension harvested real answers off a form it had filled *and* the form belongs to a
    known job — the click heuristic that catches JS-driven submits also fires on the
    "Apply" button of a listing page, and a log that records applications you did not make
    is worse than no log at all. The write is idempotent per job, so the four steps of a
    Workday form produce one application rather than four.
    """
    ctx = await _apply_context(current_user.id, body.job_id)
    banked = await mcp_client.config_mcp(
        current_user.id,
        "record_application_answers",
        {
            "answers": [a.model_dump() for a in body.answers],
            "employer_slug": ctx["employer_slug"],
            "company": ctx["company"],
        },
    )

    application = None
    if ctx["job"] and body.answers:
        try:
            res = await mcp_client.db_mcp(
                current_user.id,
                "record_application",
                {
                    "job_id": str(ctx["job"]["id"]),
                    "company": ctx["company"],
                    "job_title": ctx["job"].get("title"),
                    "employer_slug": ctx["employer_slug"],
                    "answers_recorded": len(body.answers),
                    "source": "extension",
                    **_recorded_cv(ctx, body.cv),
                },
            )
            application = res.get("application")
        except Exception:  # noqa: BLE001 - the bank already succeeded; a lost log entry
            # must not turn a submitted application into an error the user sees.
            logger.exception("failed to log application for job %s", body.job_id)

    return {**banked, "application": application}


@app.get("/applications", tags=["apply"])
async def list_applications(
    limit: int = 500, current_user=Depends(get_current_user)
) -> dict:
    """Every application logged for the caller, most recent first."""
    return await mcp_client.db_mcp(current_user.id, "get_applications", {"limit": limit})


@app.get("/applications/by-company", tags=["apply"])
async def applications_by_company(current_user=Depends(get_current_user)) -> dict:
    """Applications grouped by employer, with the count of distinct CVs sent to each.

    ``repeats`` is the answer to "am I applying to one company several times, with
    different tailored CVs?" — employers grouped by ATS board slug where available, so
    "Stripe" and "Stripe, Inc." do not read as two companies.
    """
    return await mcp_client.db_mcp(current_user.id, "application_company_summary", {})


@app.get("/answer-bank", tags=["apply"])
async def get_answer_bank(current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.config_mcp(current_user.id, "get_answer_bank", {})


@app.post("/answer-bank", tags=["apply"])
async def upsert_answer(
    body: AnswerUpsert, current_user=Depends(get_current_user)
) -> dict:
    return await mcp_client.config_mcp(
        current_user.id, "upsert_answer", body.model_dump(exclude_none=True)
    )


@app.delete("/answer-bank/{entry_id}", tags=["apply"])
async def delete_answer(entry_id: str, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.config_mcp(current_user.id, "delete_answer", {"entry_id": entry_id})


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
        return await run_turn(current_user.id, _uuid.UUID(session_id), body.text,
                              selected_job_ids=body.selected_job_ids)
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


@app.get("/resume/versions/{resume_id}/download/{fmt}", tags=["resume"])
async def download_resume_version_file(
    resume_id: str, fmt: str, current_user=Depends(get_current_user)
):
    """Download a resume version as a rendered PDF/DOCX.

    Renders through the SAME deterministic Tailor layout the tailored CVs use
    (``jobpilot_core.tailor``), so an assembled 'apply-to-resume' version comes out in the
    identical A4, ATS-safe format — no LLM, no second template to drift. Rendered on demand
    rather than stored: these versions are exports, and re-rendering is cheap.

    This does NOT make the version the working resume — ``/resumes/me/base`` still returns
    the latest *uploaded* file. To have it scored/tailored, download it and upload it back.
    """
    from fastapi.responses import Response
    from jobpilot_core import doc_meta, tailor

    if fmt not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="fmt must be pdf|docx")
    res = await mcp_client.resume_mcp(
        current_user.id, "get_resume_version", {"resume_id": resume_id})
    resume = res.get("resume")
    if not resume:
        raise HTTPException(status_code=404, detail="resume version not found")
    structured = resume.get("parsed_json") or {}
    if not structured:
        raise HTTPException(status_code=422, detail="version has no structured content to render")

    # Carry the uploaded CV's document properties, exactly as the tailored exports do, so
    # a downloaded version does not stand out from them in a properties dialog.
    meta = doc_meta.from_jsonable(
        (await mcp_client.resume_mcp(
            current_user.id, "get_resume_doc_metadata", {})).get("metadata"))

    if fmt == "pdf":
        data = tailor.render_pdf(structured, meta)
        media = "application/pdf"
    else:
        data = tailor.render_docx(structured, meta)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(
        content=data, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="resume-{resume_id[:8]}.{fmt}"'})


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
    """Queue CV alignment for the selected jobs: the Rewriter⇄Critic loop runs per job
    until the CV matches it (locked-safe, every added skill evidence-gated). Only the
    user's selected jobs are tailored — never the whole pool."""
    from .tasks import cv_tailor as task

    t = task.delay(str(current_user.id), body.job_ids)
    return {"task_id": t.id, "status": "queued", "job_count": len(body.job_ids)}


@app.post("/cv/prepare", tags=["cv"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def cv_prepare(body: CvTailorRequest, current_user=Depends(get_current_user)) -> dict:
    """One-click per-job CV: Analyze → Tailor → approve → Export for the selected jobs, so
    a downloadable tailored CV attaches to each job row (and the qualifier scores it)."""
    from .tasks import cv_prepare as task

    t = task.delay(str(current_user.id), body.job_ids)
    return {"task_id": t.id, "status": "queued", "job_count": len(body.job_ids)}


@app.post("/cv/gap-close", tags=["cv"], status_code=status.HTTP_202_ACCEPTED, dependencies=[_rl_trigger])
async def cv_gap_close(body: CvTailorRequest, current_user=Depends(get_current_user)) -> dict:
    """Phase 6.6: propose gap-closing CV content for the selected jobs — skills the résumé
    never mentions, aggressive bullet rewrites, and brand-new bullets. These are PROPOSALS
    only: nothing reaches a CV until the user accepts items individually, and an accepted
    item is recorded as the candidate's own assertion (``user_confirmed``)."""
    from .tasks import cv_gap_close as task

    t = task.delay(str(current_user.id), body.job_ids, False)
    return {"task_id": t.id, "status": "queued", "job_count": len(body.job_ids)}


@app.post("/cv/gap-close/refine", tags=["cv"], status_code=status.HTTP_202_ACCEPTED,
          dependencies=[_rl_trigger])
async def cv_gap_close_refine(
    body: CvTailorRequest, current_user=Depends(get_current_user)
) -> dict:
    """Propose, critique, then re-propose — the reviewed variant of ``/cv/gap-close``.

    A second agent grades the proposal set on defensibility (would the interview_prep
    survive a follow-up question?) and on writing quality, and the proposer answers its
    findings. Still proposals only, and still nothing reaches a CV without the user
    accepting each item. Its own endpoint rather than a flag on the first because it costs
    up to two extra model calls per job, and spending that is the user's decision."""
    from .tasks import cv_gap_close as task

    t = task.delay(str(current_user.id), body.job_ids, True)
    return {"task_id": t.id, "status": "queued", "job_count": len(body.job_ids),
            "refine": True}


@app.post("/suggestions/{suggestion_id}/gap-close/review", tags=["cv"])
async def review_gap_close(
    suggestion_id: str, body: GapCloseReviewRequest, current_user=Depends(get_current_user)
) -> dict:
    """Accept / reject / edit individual gap-close proposals. Accepting is the candidate
    attesting they can back the item in an interview — that is what admits it past the
    evidence gate on the Tailor-only path. Leaves the row's ``status`` untouched, so
    apply-to-resume keeps reading only the honest Enhancer buckets."""
    out = await mcp_client.db_mcp(
        current_user.id, "set_gap_close_items",
        {"suggestion_id": suggestion_id, "items": [i.model_dump() for i in body.items]})
    if not out.get("suggestion"):
        raise HTTPException(status_code=404, detail="suggestion not found")
    return out


@app.get("/cv/gap-close/status", tags=["cv"])
async def cv_gap_close_status(current_user=Depends(get_current_user)) -> dict:
    """Live progress of the caller's gap-close run.

    The runner has no ``pipeline_runs`` row to poll, and it can outlive any page the user has
    open — so it keeps a durable progress record instead. ``{"progress": null}`` simply means
    no run has been started (or its record has aged out)."""
    from .gap_close_runner import PROGRESS_KIND
    from .run_progress import get_progress

    return {"progress": await get_progress(PROGRESS_KIND, current_user.id)}


@app.post("/suggestions/gap-close/delete", tags=["cv"])
async def delete_gap_close(
    body: GapCloseDeleteRequest, current_user=Depends(get_current_user)
) -> dict:
    """Discard gap-close proposal sets the user doesn't want to review.

    Clears ``gap_close_json`` on those rows only. The suggestion row itself and its honest
    Enhancer buckets survive, so this can never delete résumé-grounded work as a side effect
    of tidying up proposals."""
    return await mcp_client.db_mcp(
        current_user.id, "delete_gap_close", {"suggestion_ids": body.suggestion_ids})


@app.get("/cv/drafts", tags=["cv"])
async def cv_drafts(status: str | None = None, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_cv_drafts", {"status": status})


@app.post("/cv/drafts/delete", tags=["cv"])
async def delete_cv_drafts(
    body: CvDraftDeleteRequest, current_user=Depends(get_current_user)
) -> dict:
    """Delete tailored-CV drafts and their rendered files.

    Rejecting a draft keeps it (and its history); this removes it. The jobs those drafts
    were built for are untouched, so the same job can be tailored again from scratch."""
    return await mcp_client.db_mcp(
        current_user.id, "delete_cv_drafts", {"draft_ids": body.draft_ids})


@app.get("/cv/drafts/{draft_id}", tags=["cv"])
async def cv_draft(draft_id: str, current_user=Depends(get_current_user)) -> dict:
    return await mcp_client.db_mcp(current_user.id, "get_cv_draft", {"draft_id": draft_id})


@app.post("/cv/drafts/{draft_id}/edit", tags=["cv"])
async def cv_draft_edit(
    draft_id: str, body: CvDraftEditRequest, current_user=Depends(get_current_user)
) -> dict:
    """Phase 6.6: add/remove skills and bullets on one draft before approving it. NO LLM.
    The draft returns to ``draft_ready`` and its stale PDF/DOCX detach, so an edit can never
    ship inside an already-exported file."""
    from .cv_draft_edit_runner import edit_draft

    try:
        return await edit_draft(
            current_user.id, draft_id,
            add_skills=body.add_skills, remove_skills=body.remove_skills,
            add_bullets=body.add_bullets, remove_bullets=body.remove_bullets)
    except LookupError:
        raise HTTPException(status_code=404, detail="draft not found")


@app.post("/cv/drafts/{draft_id}/reword", tags=["cv"], dependencies=[_rl_trigger])
async def cv_draft_reword(
    draft_id: str, body: CvDraftRewordRequest, current_user=Depends(get_current_user)
) -> dict:
    """Phase 6.6: reword one draft's summary or a single role's bullets to a free-text
    instruction — one focused model call, not a full re-tailor."""
    from .cv_draft_edit_runner import reword_draft

    try:
        return await reword_draft(
            current_user.id, draft_id, target=body.target, instruction=body.instruction,
            role_index=body.role_index, role_ref=body.role_ref)
    except LookupError:
        raise HTTPException(status_code=404, detail="draft not found")


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


@app.post("/query/ask/stream", tags=["query"], dependencies=[_rl_chat])
async def query_ask_stream(
    body: QueryRequest, request: Request, current_user=Depends(get_current_user)
) -> StreamingResponse:
    """Same read-only turn as ``/query/ask``, delivered as Server-Sent Events: tool calls
    surface as they complete and the answer streams token by token, instead of the whole
    turn landing at once after several model round trips.

    The session id is emitted FIRST, before any model work, so a client that started
    without one can attach the reply to the right thread even if the turn then fails.
    Once streaming has begun the status line is already sent, so a mid-turn failure can
    only be reported as an ``error`` event — never an HTTP error code."""
    import uuid as _uuid

    from .query_runner import stream_query_turn

    session_id = body.session_id
    if not session_id:
        session_id = (await mcp_client.db_mcp(
            current_user.id, "create_chat_session", {"title": "query"}))["id"]

    async def gen():
        yield f"data: {json.dumps({'kind': 'session', 'session_id': session_id})}\n\n"
        try:
            async for event in stream_query_turn(
                current_user.id, _uuid.UUID(session_id), body.question
            ):
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001 - a live stream reports, it cannot 503
            logging.getLogger("jobpilot.query").warning("query.stream failed: %s", exc)
            yield f"data: {json.dumps({'kind': 'error', 'error': str(exc)})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/query/traces", tags=["query"])
async def query_traces(
    limit: int = 100, since_days: int | None = None, current_user=Depends(get_current_user)
) -> dict:
    """The caller's recent Query turns, traced: spans, tokens, cost, and what each tool
    returned — plus the rollup the dashboard renders.

    Scoped by the request context like every other read, so a trace log is only ever
    visible to the user whose questions it records.
    """
    from .query_traces import summarize_traces

    res = await mcp_client.db_mcp(current_user.id, "list_agent_events", {
        "type": "query_turn", "limit": max(1, min(limit, 500)), "since_days": since_days})
    events = res.get("events", []) if isinstance(res, dict) else []
    traces = [
        {**(e.get("payload") or {}), "id": e.get("id"), "created_at": e.get("created_at")}
        for e in events
    ]
    return {"traces": traces, "summary": summarize_traces(traces)}


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
