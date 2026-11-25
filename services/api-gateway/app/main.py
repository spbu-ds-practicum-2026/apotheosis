import os
import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import get_auth_mode, login_user, register_user, verify_token

FEED_SERVICE_URL = os.getenv("FEED_SERVICE_URL", "http://feed:8001").rstrip("/")
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
SYSTEM_CATEGORIES = [
    "природа",
    "горы",
    "море",
    "город",
    "архитектура",
    "ночь",
    "закат",
    "осень",
    "снег",
    "космос",
    "кофе",
    "минимализм",
    "спорт",
    "киберпанк",
    "животные",
    "арт",
]
ALLOWED_CATEGORIES_SET = set(SYSTEM_CATEGORIES)

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


@app.get("/api/v1/categories")
def get_categories():
    return {"categories": SYSTEM_CATEGORIES}


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


@app.post("/api/v1/pins", status_code=201)
async def create_pin(
    title: str = Form(..., min_length=1, max_length=120),
    description: str = Form("", max_length=1000),
    tags: str = Form(...),
    event_id: str = Form(..., min_length=1),
    image: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
):
    if image.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Неподдерживаемый формат изображения. Разрешены только JPEG, PNG и WEBP.",
        )

    parsed_categories = [c.strip().lower() for c in tags.split(",") if c.strip()]
    if not parsed_categories:
        raise HTTPException(status_code=400, detail="Выберите хотя бы одну категорию.")
    invalid = [c for c in parsed_categories if c not in ALLOWED_CATEGORIES_SET]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Недопустимые категории: {', '.join(invalid)}.",
        )

    content = await image.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Файл изображения пуст.")
    if len(content) > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Размер изображения превышает 10 МБ.")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FEED_SERVICE_URL}/internal/pins",
            headers={"X-User-Id": user_id},
            data={
                "title": title.strip(),
                "description": description.strip(),
                "tags": ",".join(dict.fromkeys(parsed_categories)),
                "event_id": event_id.strip(),
            },
            files={"image": (image.filename or "pin.jpg", content, image.content_type)},
            timeout=20.0,
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


@app.get("/api/v1/users/me")
async def get_my_profile(user_id: str = Depends(get_current_user)):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{FEED_SERVICE_URL}/internal/me",
            headers={"X-User-Id": user_id},
            timeout=10.0,
        )
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.get("/api/v1/users/{target_user_id}")
async def get_public_profile(
    target_user_id: str,
    limit: int = Query(20, ge=1, le=100),
    _user_id: str = Depends(get_current_user),
):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{FEED_SERVICE_URL}/internal/users/{target_user_id}",
            params={"limit": limit},
            timeout=10.0,
        )
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.get("/api/v1/users/{target_user_id}/pins")
async def get_user_pins(
    target_user_id: str,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    _user_id: str = Depends(get_current_user),
):
    params: dict[str, str | int] = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{FEED_SERVICE_URL}/internal/users/{target_user_id}/pins",
            params=params,
            timeout=10.0,
        )
    return JSONResponse(status_code=resp.status_code, content=resp.json())
