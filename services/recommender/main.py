import os
from typing import Optional

from fastapi import FastAPI, Query

from schemas import Event, RecommendationResponse, RecommendedItem
import ranking as ranking
from store import DEFAULT_DATA_FILE, RecommenderStore

app = FastAPI(title="Recommender Service", version="0.2.0")

store = RecommenderStore.from_file(os.getenv("RECOMMENDER_DATA_FILE", str(DEFAULT_DATA_FILE)))
sessions = ranking.FeedSessions(store)


@app.get("/recommendations", response_model=RecommendationResponse)
async def recommendations(
    user_id: str,
    cursor: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
):
    cold = not store.has_history(user_id)
    items, next_cursor = sessions.get_page(user_id, cursor, limit)
    return RecommendationResponse(items=items, next_cursor=next_cursor, cold_start=cold)


@app.get("/popular", response_model=list[RecommendedItem])
async def get_popular(limit: int = Query(50, ge=1, le=200)):
    return ranking.popular(store, limit)


@app.get("/recent", response_model=list[RecommendedItem])
async def get_recent(limit: int = Query(50, ge=1, le=200)):
    return ranking.recent(store, limit)


@app.post("/events")
async def consume_event(event: Event):
    """Временная точка входа событий, пока нет брокера.

    Потребитель брокера будет вызывать тот же store.apply(): дедупликация
    по event_id и ack только после применения.
    """
    applied = store.apply(event)
    return {"event_id": event.event_id, "applied": applied, "duplicate": not applied}


@app.get("/users/{user_id}/interests")
async def interests(user_id: str):
    """Отладка: веса интересов пользователя по тегам."""
    return dict(sorted(store.user_interests(user_id).items(), key=lambda kv: -kv[1]))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "recommender", "pins": len(store.pins)}
