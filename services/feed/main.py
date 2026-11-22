import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from schemas import LOGIN_PATTERN, Event, FeedResponse, PinMetadata, utcnow
from feed_service import FeedService
from pin_store import InMemoryPinStore
from recommender_client import DirectEventPublisher, HttpRecommenderClient
from session import CurrentUser, get_current_user

RECOMMENDER_URL = os.getenv("RECOMMENDER_URL", "http://localhost:8001")

pin_store = InMemoryPinStore.from_file()
recommender = HttpRecommenderClient(RECOMMENDER_URL)
publisher = DirectEventPublisher(RECOMMENDER_URL)
feed_service = FeedService(recommender, pin_store)

likes: dict[str, tuple[str, str]] = {}                 # event_id -> (user_id, pin_id)
views: dict[str, tuple[str, str]] = {}                 # event_id -> (user_id, pin_id)
pin_idempotency: dict[tuple[str, str], str] = {}       # (user_id, event_id) -> pin_id


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await recommender.close()
    await publisher.close()


app = FastAPI(title="Feed Service", version="0.2.0", lifespan=lifespan)


class RegisterRequest(BaseModel):
    login: str = Field(..., pattern=LOGIN_PATTERN)
    password: str = Field(..., min_length=6)


class LoginRequest(BaseModel):
    login: str
    password: str


class AuthResponse(BaseModel):
    token: str
    user_id: str


class InteractionRequest(BaseModel):
    event_id: str
    author_id: str   # нужен, чтобы найти шард пина (пины шардированы по userId автора)


class CreatePinRequest(BaseModel):
    event_id: str
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    image_key: str


class CreatePinResponse(BaseModel):
    pin_id: str
    status: str


class UserProfile(BaseModel):
    user_id: str
    login: str
    pins_count: int
    likes_received: int


class MessageResponse(BaseModel):
    message: str
    duplicate: bool = False


def _issue_token(user_id: str) -> AuthResponse:
    return AuthResponse(token=f"{user_id}.{uuid.uuid4().hex}", user_id=user_id)


@app.post("/auth/register", response_model=AuthResponse)
async def register(req: RegisterRequest):
    return _issue_token(req.login)


@app.post("/auth/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    return _issue_token(req.login)


@app.get("/feed", response_model=FeedResponse)
async def get_feed(
    cursor: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    user: CurrentUser = Depends(get_current_user),
):
    return await feed_service.get_portion(user.user_id, cursor, limit)


async def _record(kind: str, table: dict, pin_id: str, req: InteractionRequest, user: CurrentUser) -> MessageResponse:
    if pin_store.get(req.author_id, pin_id) is None:
        raise HTTPException(status_code=404, detail="Pin not found")
    if req.event_id in table:  # повтор с тем же event_id: результат первой операции
        return MessageResponse(message=f"{kind} already accepted", duplicate=True)
    table[req.event_id] = (user.user_id, pin_id)
    await publisher.publish(Event(event_id=req.event_id, type=kind, user_id=user.user_id,
                                  pin_id=pin_id, author_id=req.author_id))
    # в целевой схеме кэш инвалидирует Recommender после применения события
    feed_service.invalidate_user(user.user_id)
    return MessageResponse(message=f"{kind} accepted")


@app.post("/pins/{pin_id}/like", response_model=MessageResponse)
async def like_pin(pin_id: str, req: InteractionRequest, user: CurrentUser = Depends(get_current_user)):
    return await _record("like", likes, pin_id, req, user)


@app.post("/pins/{pin_id}/view", response_model=MessageResponse)
async def view_pin(pin_id: str, req: InteractionRequest, user: CurrentUser = Depends(get_current_user)):
    return await _record("view", views, pin_id, req, user)


@app.post("/pins", response_model=CreatePinResponse)
async def create_pin(req: CreatePinRequest, user: CurrentUser = Depends(get_current_user)):
    key = (user.user_id, req.event_id)
    if key in pin_idempotency:
        return CreatePinResponse(pin_id=pin_idempotency[key], status="created")
    pin_id = str(uuid.uuid4())
    pin_idempotency[key] = pin_id
    tags = sorted({t.strip().lower() for t in req.tags if t.strip()})
    pin_store.add(PinMetadata(pin_id=pin_id, author_id=user.user_id, image_key=req.image_key,
                              title=req.title, description=req.description, tags=tags,
                              created_at=utcnow()))
    await publisher.publish(Event(event_id=req.event_id, type="pin_created", user_id=user.user_id,
                                  pin_id=pin_id, author_id=user.user_id, tags=tags))
    return CreatePinResponse(pin_id=pin_id, status="created")


@app.get("/users/{user_id}", response_model=UserProfile)
async def get_user_profile(user_id: str):
    own = {p.pin_id for p in pin_store.by_author(user_id)}
    return UserProfile(
        user_id=user_id,
        login=user_id,
        pins_count=len(own),
        likes_received=sum(1 for _, pid in likes.values() if pid in own),
    )


@app.get("/users/{user_id}/pins", response_model=list[PinMetadata])
async def get_user_pins(user_id: str):
    return pin_store.by_author(user_id)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "feed", "version": "0.2.0",
            "recommender_breaker": feed_service.breaker.state}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
