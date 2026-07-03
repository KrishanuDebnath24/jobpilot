"""DB-backed authentication (replaces the Phase-0 in-memory user)."""

from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jobpilot_core import repositories
from jobpilot_core.models import User
from jobpilot_core.security import decode_access_token
from jobpilot_core.settings import Settings, get_settings
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        user_id = decode_access_token(token, settings)
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise credentials_error from exc

    user = await repositories.get_user_by_id(db, user_id)
    if user is None:
        raise credentials_error
    return user
