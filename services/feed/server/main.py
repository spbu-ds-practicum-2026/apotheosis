"""HTTP API сервиса Feed для API Gateway (порт 8001).

Контракт задаёт gateway (services/api-gateway/app/main.py):
- GET  /internal/feed?cursor=<int>&limit=<int>, заголовок X-User-Id
- POST /internal/pins (multipart: title, description, tags, event_id, image)
- POST /internal/pins/{pinId}/like | /view  ({"event_id", "author_id"})
Формат порции ленты — docs/api.md, раздел 4.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import SYSTEM_CATEGORIES
from app.storage import storage
from feed.db.connection import node_ids
from feed.profiles import get_own_profile, get_public_profile, list_publications
from feed_service import FeedService
from recommender_client import HttpRecommenderClient

from server import config, repository
from server.adapters import CursorMap, DbPinStore, RedisCache
from server.relay import OutboxRelay
from server.seed import seed_with_retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("feed")

cache = RedisCache()
cursors = CursorMap(cache)
recommender = HttpRecommenderClient(config.RECOMMENDER_URL)
feed_service = FeedService(recommender, DbPinStore(), cache=cache)
relay = OutboxRelay()


def _wait_for_db(attempts: int = 30, delay: float = 2.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            repository.apply_extra_schema()
            return
        except Exception as exc:
            log.warning("Feed DB недоступна (попытка %d): %s", attempt, exc)
            time.sleep(delay)
    raise RuntimeError("Feed DB так и не стала доступна")


def _background_start() -> None:
    try:
        storage.ensure_bucket()
    except Exception:
        log.exception("MinIO: бакет не создан")
    if config.SEED_DEMO:
        seed_with_retry(storage)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await run_in_threadpool(_wait_for_db)
    relay.start()
    threading.Thread(target=_background_start, name="seed", daemon=True).start()
    yield
    relay.stop()
    await recommender.close()


app = FastAPI(title="Feed Service", version="1.0.0", lifespan=lifespan)


@app.exception_handler(repository.FeedError)
async def _feed_error(_request, exc: repository.FeedError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unexpected(_request, exc: Exception):
    log.exception("ошибка обработки запроса")
    return JSONResponse(
        status_code=503,
        content={"detail": "Сервис временно недоступен, повторите запрос с тем же event_id"},
    )


class ActionRequest(BaseModel):
    event_id: str = Field(..., min_length=1)
    author_id: str | None = None


# ---------------------------------------------------------------- служебные

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "feed",
        "shards": node_ids(),
        "recommender_breaker": feed_service.breaker.state,
    }


@app.get("/internal/categories")
def list_categories():
    return {"categories": SYSTEM_CATEGORIES}


@app.get("/internal/shards")
def list_shards():
    return {"shards": node_ids()}


# ---------------------------------------------------------------- лента

def _to_items(user_id: str, pins) -> list[dict]:
    pairs = [(p.author_id, p.pin_id) for p in pins]
    stats = repository.like_stats(user_id, pairs)
    items = []
    for p in pins:
        likes, mine = stats.get(p.pin_id, (0, False))
        items.append({
            "pinId": p.pin_id,
            "authorId": p.author_id,
            "title": p.title,
            "description": p.description or "",
            "tags": p.tags,
            "image_key": p.image_key,
            "image_url": storage.get_presigned_url(p.image_key),
            "likes_count": likes,
            "liked_by_me": mine,
            "created_at": p.created_at.isoformat(),
            "shard": repository.shard_of(p.author_id),
        })
    return items


@app.get("/internal/feed")
async def get_feed(
    x_user_id: str = Header(..., alias="X-User-Id"),
    cursor: int = Query(0, ge=0),
    limit: int = Query(4, ge=1, le=50),
):
    real_cursor, _found = await run_in_threadpool(cursors.resolve, x_user_id, cursor)
    # курсор истёк (или потерян вместе с Redis): лента начинается заново
    portion = await feed_service.get_portion(x_user_id, real_cursor, limit)
    items = await run_in_threadpool(_to_items, x_user_id, portion.pins)
    has_more = bool(items) and portion.next_cursor is not None
    next_cursor = None
    if has_more:
        await run_in_threadpool(cursors.remember, x_user_id, cursor + 1, portion.next_cursor)
        next_cursor = str(cursor + 1)
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "degraded": portion.source != "recommender",
    }


# ---------------------------------------------------------------- пины

def _pin_item(pin: dict, likes: int = 0, mine: bool = False) -> dict:
    return {
        "pinId": pin["pin_id"],
        "authorId": pin["author_id"],
        "title": pin["title"],
        "description": pin["description"],
        "tags": pin["tags"],
        "image_key": pin["image_key"],
        "image_url": storage.get_presigned_url(pin["image_key"]),
        "likes_count": likes,
        "liked_by_me": mine,
        "created_at": pin["created_at"].isoformat(),
        "shard": repository.shard_of(pin["author_id"]),
    }


@app.post("/internal/pins", status_code=201)
def create_pin(
    x_user_id: str = Header(..., alias="X-User-Id"),
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form(...),
    event_id: str = Form(...),
    image: UploadFile = File(...),
):
    pin, created = repository.create_pin(
        storage=storage,
        author_id=x_user_id,
        title=title,
        description=description,
        tags=tags.split(","),
        event_id=event_id.strip(),
        filename=image.filename,
        data=image.file.read(),
        content_type=image.content_type or "application/octet-stream",
    )
    stats = repository.like_stats(x_user_id, [(pin["author_id"], pin["pin_id"])])
    likes, mine = stats.get(pin["pin_id"], (0, False))
    body = _pin_item(pin, likes, mine)
    # повтор с тем же event_id: результат первой операции, второй пин не создан
    return JSONResponse(status_code=201 if created else 200, content=body)


@app.post("/internal/pins/{pin_id}/like")
def like_pin(pin_id: str, body: ActionRequest, x_user_id: str = Header(..., alias="X-User-Id")):
    return repository.record_interaction("like", x_user_id, pin_id, body.event_id, body.author_id)


@app.post("/internal/pins/{pin_id}/view")
def view_pin(pin_id: str, body: ActionRequest, x_user_id: str = Header(..., alias="X-User-Id")):
    return repository.record_interaction("view", x_user_id, pin_id, body.event_id, body.author_id)


# ---------------------------------------------------------------- профили

def _with_urls(pins: list[dict]) -> list[dict]:
    return [{**p, "image_url": storage.get_presigned_url(p["image_key"])} for p in pins]


@app.get("/internal/me")
def my_profile(x_user_id: str = Header(..., alias="X-User-Id")):
    repository.ensure_user(x_user_id)
    profile = get_own_profile(x_user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return profile


@app.get("/internal/users/{user_id}")
def public_profile(user_id: str, limit: int = Query(20, ge=1, le=100)):
    profile = get_public_profile(user_id, limit)
    if profile is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    profile["pins"] = _with_urls(profile["pins"])
    return profile


@app.get("/internal/users/{user_id}/pins")
def user_pins(user_id: str, limit: int = Query(20, ge=1, le=100), cursor: str | None = None):
    try:
        page = list_publications(user_id, limit, cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    page["items"] = _with_urls(page["items"])
    return page
