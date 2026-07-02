"""Auth round-trip: login issues a JWT; the token unlocks a protected route."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_login_returns_token(client: TestClient, credentials: dict[str, str]) -> None:
    resp = client.post("/auth/login", json=credentials)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_login_rejects_bad_password(
    client: TestClient, credentials: dict[str, str]
) -> None:
    resp = client.post(
        "/auth/login", json={**credentials, "password": "wrong"}
    )
    assert resp.status_code == 401


def test_me_requires_auth(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 401


def test_me_with_token(client: TestClient, credentials: dict[str, str]) -> None:
    token = client.post("/auth/login", json=credentials).json()["access_token"]
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == credentials["email"]


def test_me_rejects_garbage_token(client: TestClient) -> None:
    resp = client.get(
        "/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert resp.status_code == 401
