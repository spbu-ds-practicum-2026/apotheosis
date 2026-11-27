import os
import httpx
from fastapi import HTTPException


def get_auth_mode() -> str:
    return os.getenv("AUTH_MODE", "keycloak").lower()


def _keycloak_url() -> str:
    return os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")


def _keycloak_realm() -> str:
    return os.getenv("KEYCLOAK_REALM", "apotheosis")


def _keycloak_client_id() -> str:
    return os.getenv("KEYCLOAK_CLIENT_ID", "apotheosis-desktop")


def _keycloak_admin_user() -> str:
    return os.getenv("KEYCLOAK_ADMIN_USER", "admin")


def _keycloak_admin_password() -> str:
    return os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")


async def _get_admin_token(client: httpx.AsyncClient) -> str:
    url = f"{_keycloak_url()}/realms/master/protocol/openid-connect/token"
    resp = await client.post(
        url,
        data={
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": _keycloak_admin_user(),
            "password": _keycloak_admin_password(),
        },
        timeout=10.0,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=503, detail="Сервис аутентификации временно недоступен")
    return resp.json()["access_token"]


async def login_user(username: str, password: str) -> dict:
    if get_auth_mode() == "stub":
        return {
            "userId": username,
            "access_token": f"stub:{username}",
            "token_type": "bearer",
            "expires_in": 3600,
        }

    url = f"{_keycloak_url()}/realms/{_keycloak_realm()}/protocol/openid-connect/token"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            data={
                "client_id": _keycloak_client_id(),
                "grant_type": "password",
                "scope": "openid",
                "username": username,
                "password": password,
            },
            timeout=10.0,
        )
    if resp.status_code in (400, 401):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if resp.status_code != 200:
        raise HTTPException(status_code=503, detail="Ошибка сервиса аутентификации")

    data = resp.json()
    return {
        "userId": username,
        "access_token": data["access_token"],
        "token_type": data.get("token_type", "bearer"),
        "expires_in": data.get("expires_in", 3600),
    }


async def register_user(username: str, password: str) -> dict:
    if get_auth_mode() == "stub":
        return await login_user(username, password)

    async with httpx.AsyncClient() as client:
        admin_token = await _get_admin_token(client)
        create_url = f"{_keycloak_url()}/admin/realms/{_keycloak_realm()}/users"
        resp = await client.post(
            create_url,
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "username": username,
                "enabled": True,
                "email": f"{username}@apotheosis.local",
                "emailVerified": True,
                "firstName": username,
                "lastName": "User",
                "requiredActions": [],
                "credentials": [
                    {"type": "password", "value": password, "temporary": False}
                ],
            },
            timeout=10.0,
        )

    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="Логин уже занят")
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=400, detail="Не удалось создать пользователя")

    return await login_user(username, password)


async def verify_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Требуется токен авторизации")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Пустой токен авторизации")

    if get_auth_mode() == "stub":
        if token.startswith("stub:") and len(token) > 5:
            return token.removeprefix("stub:")
        return "demo_user"

    userinfo_url = f"{_keycloak_url()}/realms/{_keycloak_realm()}/protocol/openid-connect/userinfo"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Сессия недействительна или истекла")

    user_id = resp.json().get("preferred_username")
    if not user_id:
        raise HTTPException(status_code=401, detail="В токене отсутствует userId")
    return user_id
