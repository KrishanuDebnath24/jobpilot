"""Seed the test user into the DB (idempotent). Run: python -m scripts.seed_user

Credentials come from TEST_USER_EMAIL / TEST_USER_PASSWORD (see .env).
"""

from __future__ import annotations

import asyncio

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.security import hash_password
from jobpilot_core.settings import get_settings


# Ireland-focused default so Scout has something sensible to fan out on. Targets a
# couple of Dublin-HQ'd employers with public Greenhouse boards for the ATS path.
DEFAULT_SEARCH_CONFIG = {
    "roles": ["Software Engineer", "Backend Engineer"],
    # Dublin (Republic of Ireland) is served by Greenhouse + the crawler; Belfast
    # (Northern Ireland) is where Adzuna's `gb` coverage lands island-of-Ireland jobs.
    "locations": ["Dublin", "Belfast"],
    "keywords": ["python"],
    # Adzuna (NI), Greenhouse (ATS/RoI), crawler (RoI), + free aggregators for RoI
    # breadth. JSearch is metered, so it stays OFF until a user opts in.
    "sources": ["adzuna", "greenhouse", "crawl", "jooble", "careerjet"],
    "target_employers": ["intercom", "stripe"],
    "schedule": None,
}


async def seed() -> None:
    settings = get_settings()
    async with session_scope() as session:
        existing = await repositories.get_user_by_email(session, settings.test_user_email)
        if existing is not None:
            user = existing
            print(f"user already exists: {settings.test_user_email} ({existing.id})")
        else:
            user = await repositories.create_user(
                session,
                settings.test_user_email,
                hash_password(settings.test_user_password),
            )
            print(f"seeded user: {settings.test_user_email} ({user.id})")

        # Seed a default search config only if the user has none (idempotent).
        cfg = await repositories.get_search_config(session, user.id)
        if cfg is None:
            await repositories.upsert_search_config(session, user.id, DEFAULT_SEARCH_CONFIG)
            print(f"seeded default search config for {settings.test_user_email}")
        else:
            print("search config already present; leaving as-is")


if __name__ == "__main__":
    asyncio.run(seed())
