"""The security core: derive the caller's user_id ONLY from a signed request
context, never from tool arguments.

An ASGI middleware validates the ``Authorization: Bearer <mcp-ctx-token>`` header
on every HTTP request and stashes the resulting user_id in a contextvar. Tools
call :func:`require_user_id` to read it. Any ``user_id`` a caller passes as a tool
argument is ignored — see the db-mcp tools. Unauthenticated requests have no
user_id in context, so every scoped tool refuses them.
"""

from __future__ import annotations

import contextvars
import uuid

import jwt

from .security import validate_mcp_context_token
from .settings import Settings

_current_user_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "jobpilot_current_user_id", default=None
)


class Unauthorized(Exception):
    """Raised by scoped tools when no valid request context is present."""


def require_user_id() -> uuid.UUID:
    """Return the trusted, context-derived user_id or raise :class:`Unauthorized`."""
    user_id = _current_user_id.get()
    if user_id is None:
        raise Unauthorized(
            "no authenticated request context; the caller must present a valid "
            "MCP context token (user_id is never taken from tool arguments)"
        )
    return user_id


def _extract_bearer(scope) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            raw = value.decode("latin-1")
            if raw.lower().startswith("bearer "):
                return raw[7:].strip()
    return None


class UserContextMiddleware:
    """Pure-ASGI middleware: validate the context token, bind user_id for the request."""

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        user_id: uuid.UUID | None = None
        token = _extract_bearer(scope)
        if token:
            try:
                user_id = validate_mcp_context_token(token, self.settings)
            except (jwt.PyJWTError, ValueError, KeyError):
                user_id = None  # invalid/expired -> unauthenticated

        ctx_token = _current_user_id.set(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user_id.reset(ctx_token)
