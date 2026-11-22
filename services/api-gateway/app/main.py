import os
import httpx
from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import get_auth_mode, login_user, register_user, verify_token

FEED_SERVICE_URL = os.getenv("FEED_SERVICE_URL", "http://feed:8001").rstrip("/")

app = FastAPI(title="Apotheosis API Gateway")


class AuthCredentials(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=4, max_length=128)


class EventPayload(BaseModel):
    event_id: str = Field(..., min_length=1)
    author_id: str | None = None


async def get_current_user(authorization: str | None = Header(default=None)) -> str:
    return await verify_token(authorization)


@app.get("/")
def root():
    return {"service": "api-gateway", "status": "ok", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "api-gateway", "auth_mode": get_auth_mode()}


@app.post("/api/v1/auth/register", status_code=201)
async def auth_register(body: AuthCredentials):
    return await register_user(body.username, body.password)


@app.post("/api/v1/auth/login")
async def auth_login(body: AuthCredentials):
    return await login_user(body.username, body.password)


@app.get("/api/v1/auth/me")
async def auth_me(user_id: str = Depends(get_current_user)):
    return {"userId": user_id}


@app.get("/api/v1/feed")
async def get_feed(
    cursor: int = Query(0, ge=0),
    limit: int = Query(4, ge=1, le=50),
    user_id: str = Depends(get_current_user),
):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{FEED_SERVICE_URL}/internal/feed",
            params={"cursor": cursor, "limit": limit},
            headers={"X-User-Id": user_id},
            timeout=10.0,
        )
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.post("/api/v1/pins/{pin_id}/like")
async def like_pin(
    pin_id: str,
    body: EventPayload,
    user_id: str = Depends(get_current_user),
):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FEED_SERVICE_URL}/internal/pins/{pin_id}/like",
            headers={"X-User-Id": user_id},
            json=body.model_dump(),
            timeout=10.0,
        )
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.post("/api/v1/pins/{pin_id}/view")
async def view_pin(
    pin_id: str,
    body: EventPayload,
    user_id: str = Depends(get_current_user),
):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FEED_SERVICE_URL}/internal/pins/{pin_id}/view",
            headers={"X-User-Id": user_id},
            json=body.model_dump(),
            timeout=10.0,
        )
    return JSONResponse(status_code=resp.status_code, content=resp.json())
