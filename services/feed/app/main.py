from fastapi import FastAPI, Header, HTTPException, Query

from app.models import ActionRequest, AddShardRequest, FeedResponse
from app.repository import repo
from app.routing import default_ring

app = FastAPI(title="Feed Service")


@app.get("/health")
def health():
    return {"status": "ok", "service": "feed", "shards": default_ring.list_shards()}


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
