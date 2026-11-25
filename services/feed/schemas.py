from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Логин = userId = ключ шардирования. Точка запрещена, потому что токен
# сессии имеет вид <userId>.<случайная_строка>.
LOGIN_PATTERN = r"^[A-Za-z0-9_-]{3,30}$"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PinMetadata(BaseModel):
    pin_id: str
    author_id: str          # пины шардируются по author_id (userId автора)
    image_key: str          # ключ объекта в MinIO: pins/<pinId>/<filename>
    title: str
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime


FeedSource = Literal["recommender", "popular", "recent"]


class FeedResponse(BaseModel):
    """Порция ленты, которую Feed отдаёт клиенту."""
    pins: list[PinMetadata]
    next_cursor: Optional[str] = None   # непрозрачная строка; передать в следующий запрос
    source: FeedSource = "recommender"  # откуда порция: персональная или запасной путь


class RecommendedItem(BaseModel):
    pin_id: str
    author_id: str    # нужен Feed, чтобы найти шард пина
    score: float


class RecommendationResponse(BaseModel):
    items: list[RecommendedItem]
    next_cursor: Optional[str] = None
    cold_start: bool = False  # True — у пользователя нет истории, выдано популярное


EventType = Literal["like", "view", "pin_created"]


class Event(BaseModel):
    event_id: str             # UUID, создаёт клиент; ключ дедупликации
    type: EventType
    user_id: str             # кто совершил действие (для pin_created — автор)
    pin_id: str
    author_id: str              # автор пина (шард пина)
    tags: list[str] = Field(default_factory=list)  # заполняется только для pin_created
    occurred_at: datetime = Field(default_factory=utcnow)
