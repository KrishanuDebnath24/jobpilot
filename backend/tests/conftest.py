"""Shared test fixtures. Pins deterministic auth env before the app imports settings."""

from __future__ import annotations

import os

# Set env BEFORE importing the app so get_settings() picks these up.
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("TEST_USER_EMAIL", "test@jobpilot.dev")
os.environ.setdefault("TEST_USER_PASSWORD", "changeme123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def credentials() -> dict[str, str]:
    return {"email": "test@jobpilot.dev", "password": "changeme123"}
