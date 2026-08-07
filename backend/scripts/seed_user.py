"""Seed the test user into the DB (idempotent). Run: python -m scripts.seed_user

Credentials come from TEST_USER_EMAIL / TEST_USER_PASSWORD (see .env).
"""

from __future__ import annotations

import asyncio

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.security import hash_password
from jobpilot_core.settings import get_settings


# Republic-of-Ireland default so Scout has something sensible to fan out on.
DEFAULT_SEARCH_CONFIG = {
    "roles": ["Software Engineer", "Backend Engineer"],
    # Republic of Ireland only. Belfast was here while Adzuna supplied "gb" coverage;
    # Adzuna is gone and Northern Ireland is a different jurisdiction (different work
    # authorisation and tax), so it is deliberately not seeded.
    "locations": ["Dublin"],
    "keywords": ["python"],
    # All full-text and RoI-filtered. Workable needs no employer list and is the
    # broadest source; Greenhouse carries the verified Irish boards. JSearch and Apify
    # are metered, so both stay OFF until a user opts in.
    "sources": ["workable", "greenhouse", "crawl", "muse"],
    # `target_employers` extends the curated Greenhouse board list in
    # jobpilot_core.ats_boards with the user's own picks. A slug that doesn't resolve is
    # recorded as a connector error, never fatal — but verify against a live board
    # (boards.greenhouse.io/<slug>) *and* check the job locations, because a name-derived
    # slug resolves to the wrong company often enough to matter.
    "target_employers": [
        "workhuman",
        "wayflyer",
        "hubspot",
    ],
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
