from contextlib import asynccontextmanager
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile

from app.config import SYSTEM_CATEGORIES
from app.models import ActionRequest, AddShardRequest, FeedResponse, PinItem
from app.repository import repo
from app.routing import default_ring
from app.storage import storage


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.ensure_bucket()
    repo.seed_demo_pins()
    yield


app = FastAPI(title="Feed Service", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "feed", "shards": default_ring.list_shards()}


@app.get("/internal/categories")
def list_categories():
    return {"categories": SYSTEM_CATEGORIES}


@app.get("/internal/shards")
def list_shards():
    return {"shards": default_ring.list_shards()}


@app.post("/internal/shards")
def add_shard(body: AddShardRequest):
    return repo.add_shard(body.shard_id)


@app.get("/internal/feed", response_model=FeedResponse)
def get_feed(
    x_user_id: str = Header(..., alias="X-User-Id"),
    cursor: int = Query(0, ge=0),
    limit: int = Query(4, ge=1, le=50),
):
    items, next_cursor, has_more = repo.list_feed(x_user_id, cursor, limit)
    return FeedResponse(items=items, next_cursor=next_cursor, has_more=has_more, degraded=False)


@app.post("/internal/pins", response_model=PinItem, status_code=201)
async def create_pin(
    x_user_id: str = Header(..., alias="X-User-Id"),
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form(...),
    event_id: str = Form(...),
    image: UploadFile = File(...),
):
    parsed_tags = [t.strip().lower() for t in tags.split(",") if t.strip()]
    file_bytes = await image.read()
    pin = repo.create_pin(
        author_id=x_user_id,
        title=title.strip(),
        description=description.strip(),
        tags=parsed_tags,
        event_id=event_id.strip(),
        filename=image.filename or "pin.png",
        file_bytes=file_bytes,
        content_type=image.content_type or "application/octet-stream",
    )
    return PinItem(
        pinId=pin["pinId"],
        authorId=pin["authorId"],
        title=pin["title"],
        description=pin["description"],
        tags=pin["tags"],
        image_key=pin["image_key"],
        image_url=storage.get_presigned_url(pin["image_key"]),
        likes_count=pin["likes_count"],
        liked_by_me=False,
        created_at=pin["created_at"],
        shard=pin["shard"],
    )


@app.post("/internal/pins/{pin_id}/like")
def like_pin(
    pin_id: str,
    body: ActionRequest,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    result = repo.add_like(x_user_id, pin_id, body.event_id, body.author_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Пин не найден")
    return result


@app.post("/internal/pins/{pin_id}/view")
def view_pin(
    pin_id: str,
    body: ActionRequest,
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    result = repo.add_view(x_user_id, pin_id, body.event_id, body.author_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Пин не найден")
    return result
