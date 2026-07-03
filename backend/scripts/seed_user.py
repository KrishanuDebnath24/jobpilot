"""Seed the test user into the DB (idempotent). Run: python -m scripts.seed_user

Credentials come from TEST_USER_EMAIL / TEST_USER_PASSWORD (see .env).
"""

from __future__ import annotations

import asyncio

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.security import hash_password
from jobpilot_core.settings import get_settings


async def seed() -> None:
    settings = get_settings()
    async with session_scope() as session:
        existing = await repositories.get_user_by_email(session, settings.test_user_email)
        if existing is not None:
            print(f"user already exists: {settings.test_user_email} ({existing.id})")
            return
        user = await repositories.create_user(
            session,
            settings.test_user_email,
            hash_password(settings.test_user_password),
        )
        print(f"seeded user: {settings.test_user_email} ({user.id})")


if __name__ == "__main__":
    asyncio.run(seed())
