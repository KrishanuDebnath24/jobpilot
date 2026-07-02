"""Phase 0 authentication: JWT + a single in-memory test user.

Deliberately DB-free. The real `users` table, registration, and per-user isolation
land in Phase 1; the interface here (login -> token -> `get_current_user`) is designed
so swapping the in-memory store for the DB later is a localized change.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr

from .config import Settings, get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


class User(BaseModel):
    id: str
    email: EmailStr


class _StoredUser(BaseModel):
    id: str
    email: EmailStr
    password_hash: bytes


# ---------------------------------------------------------------------------
# Password hashing (bcrypt directly — see DECISIONS.md)
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> bytes:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())


def verify_password(plain: str, password_hash: bytes) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), password_hash)


# ---------------------------------------------------------------------------
# In-memory user store (Phase 0 only)
# ---------------------------------------------------------------------------
class UserStore:
    """One seeded test user. Replaced by the DB-backed store in Phase 1."""

    def __init__(self, settings: Settings) -> None:
        self._by_email: dict[str, _StoredUser] = {}
        self._seed(settings)

    def _seed(self, settings: Settings) -> None:
        email = settings.test_user_email.lower()
        self._by_email[email] = _StoredUser(
            id=str(uuid.uuid4()),
            email=email,
            password_hash=hash_password(settings.test_user_password),
        )

    def authenticate(self, email: str, password: str) -> User | None:
        stored = self._by_email.get(email.lower())
        if stored is None or not verify_password(password, stored.password_hash):
            return None
        return User(id=stored.id, email=stored.email)

    def get_by_email(self, email: str) -> User | None:
        stored = self._by_email.get(email.lower())
        return None if stored is None else User(id=stored.id, email=stored.email)


_store: UserStore | None = None


def get_user_store(settings: Settings = Depends(get_settings)) -> UserStore:
    global _store
    if _store is None:
        _store = UserStore(settings)
    return _store


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def create_access_token(subject: str, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
    store: UserStore = Depends(get_user_store),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        email = payload.get("sub")
        if not email:
            raise credentials_error
    except jwt.PyJWTError as exc:  # expired / invalid signature / malformed
        raise credentials_error from exc

    user = store.get_by_email(email)
    if user is None:
        raise credentials_error
    return user
