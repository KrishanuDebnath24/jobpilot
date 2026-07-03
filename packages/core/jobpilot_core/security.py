"""Password hashing, user access tokens, and MCP request-context tokens.

Two distinct token kinds, both HS256:
- **User access token** (login): subject = user_id, verified by the backend.
- **MCP context token**: minted by the backend per outbound MCP call, subject =
  user_id, signed with ``mcp_shared_secret``. The MCP service validates it and
  derives ``user_id`` *only* from this token — never from tool arguments.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .settings import Settings

_MCP_TOKEN_TYPE = "mcp-ctx"


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))


# --------------------------------------------------------------------------- #
# User access tokens (login)
# --------------------------------------------------------------------------- #
def create_access_token(user_id: uuid.UUID, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    """Return the user_id, or raise jwt.PyJWTError."""
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return uuid.UUID(payload["sub"])


# --------------------------------------------------------------------------- #
# MCP request-context tokens (backend -> MCP service)
# --------------------------------------------------------------------------- #
def mint_mcp_context_token(user_id: uuid.UUID, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "typ": _MCP_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(seconds=settings.mcp_context_ttl_seconds),
    }
    return jwt.encode(payload, settings.effective_mcp_secret(), algorithm="HS256")


def validate_mcp_context_token(token: str, settings: Settings) -> uuid.UUID:
    """Return the trusted user_id from a signed MCP context token, or raise."""
    payload = jwt.decode(token, settings.effective_mcp_secret(), algorithms=["HS256"])
    if payload.get("typ") != _MCP_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not an MCP context token")
    return uuid.UUID(payload["sub"])
