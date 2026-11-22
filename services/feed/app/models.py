from pydantic import BaseModel, Field


class ActionRequest(BaseModel):
    event_id: str = Field(..., min_length=1)
    author_id: str | None = None


class AddShardRequest(BaseModel):
    shard_id: str = Field(..., min_length=1)


class PinItem(BaseModel):
    pinId: str
    authorId: str
    title: str
    description: str
    tags: list[str]
    image_url: str
    likes_count: int
    liked_by_me: bool
    created_at: str
    shard: str


class FeedResponse(BaseModel):
    items: list[PinItem]
    next_cursor: str | None
    has_more: bool
    degraded: bool = False
