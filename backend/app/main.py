"""JobPilot FastAPI app — Phase 0: health check + JWT auth (one test user)."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

from .auth import (
    User,
    UserStore,
    create_access_token,
    get_current_user,
    get_user_store,
)
from .config import Settings, get_settings

settings = get_settings()

app = FastAPI(
    title="JobPilot API",
    version=settings.version,
    summary="Agentic job-hunting platform — Phase 0 scaffold.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    """Liveness probe. Phase 0 gate: must pass across both services."""
    return HealthResponse(service=settings.app_name, version=settings.version)


@app.post("/auth/login", response_model=TokenResponse, tags=["auth"])
def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
    store: UserStore = Depends(get_user_store),
) -> TokenResponse:
    user = store.authenticate(body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    return TokenResponse(access_token=create_access_token(user.email, settings))


@app.get("/auth/me", response_model=User, tags=["auth"])
def read_me(current_user: User = Depends(get_current_user)) -> User:
    """Protected route — proves the JWT round-trip works."""
    return current_user
