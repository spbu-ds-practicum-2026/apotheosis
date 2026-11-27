import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

GATEWAY_ROOT = Path(__file__).resolve().parents[1] / "services" / "api-gateway"
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.main import app

client = TestClient(app)


def _mock_response(status_code: int, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


def test_stub_mode_accepts_any_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "stub")

    r_any = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer any-random-token"})
    assert r_any.status_code == 200
    assert r_any.json() == {"userId": "demo_user"}

    r_named = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer stub:alice"})
    assert r_named.status_code == 200
    assert r_named.json() == {"userId": "alice"}


def test_missing_or_malformed_token_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "keycloak")

    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_register_and_login_input_validation():
    bad_user = client.post("/api/v1/auth/register", json={"username": "ab", "password": "secret"})
    assert bad_user.status_code == 422

    bad_pass = client.post("/api/v1/auth/login", json={"username": "alice", "password": "123"})
    assert bad_pass.status_code == 422


def test_keycloak_register_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "keycloak")

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        _mock_response(200, {"access_token": "admin-jwt"}),
        _mock_response(201, {}),
        _mock_response(200, {"access_token": "user-jwt", "token_type": "bearer", "expires_in": 3600}),
    ]
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client

    with patch("app.auth.httpx.AsyncClient", return_value=mock_cm):
        resp = client.post("/api/v1/auth/register", json={"username": "bogdan", "password": "password123"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["userId"] == "bogdan"
    assert body["access_token"] == "user-jwt"


def test_keycloak_register_duplicate_login_returns_409(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "keycloak")

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        _mock_response(200, {"access_token": "admin-jwt"}),
        _mock_response(409, {"errorMessage": "User exists"}),
    ]
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client

    with patch("app.auth.httpx.AsyncClient", return_value=mock_cm):
        resp = client.post("/api/v1/auth/register", json={"username": "bogdan", "password": "password123"})

    assert resp.status_code == 409


def test_keycloak_login_success_and_wrong_password(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "keycloak")

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        _mock_response(200, {"access_token": "valid-jwt", "token_type": "bearer", "expires_in": 3600}),
        _mock_response(401, {"error": "invalid_grant"}),
    ]
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client

    with patch("app.auth.httpx.AsyncClient", return_value=mock_cm):
        ok_resp = client.post("/api/v1/auth/login", json={"username": "bogdan", "password": "password123"})
        bad_resp = client.post("/api/v1/auth/login", json={"username": "bogdan", "password": "wrong"})

    assert ok_resp.status_code == 200
    assert ok_resp.json()["access_token"] == "valid-jwt"
    assert bad_resp.status_code == 401


def test_keycloak_me_valid_and_expired_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "keycloak")

    mock_client = AsyncMock()
    mock_client.get.side_effect = [
        _mock_response(200, {"preferred_username": "bogdan"}),
        _mock_response(401, {"error": "invalid_token"}),
    ]
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client

    with patch("app.auth.httpx.AsyncClient", return_value=mock_cm):
        ok_resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer valid-jwt"})
        exp_resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer expired-jwt"})

    assert ok_resp.status_code == 200
    assert ok_resp.json() == {"userId": "bogdan"}
    assert exp_resp.status_code == 401


def test_upload_validates_image_type_and_categories(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_MODE", "stub")

    bad_file = client.post(
        "/api/v1/pins",
        headers={"Authorization": "Bearer stub:bogdan"},
        data={"title": "Test", "description": "", "tags": "горы", "event_id": "evt-1"},
        files={"image": ("script.txt", b"hello", "text/plain")},
    )
    assert bad_file.status_code == 400

    bad_cat = client.post(
        "/api/v1/pins",
        headers={"Authorization": "Bearer stub:bogdan"},
        data={"title": "Test", "description": "", "tags": "рандомный_тег", "event_id": "evt-2"},
        files={"image": ("photo.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert bad_cat.status_code == 400
