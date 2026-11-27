"""Точка запуска Recommender в общем стенде: uvicorn server:app (порт 8002).

Берёт HTTP API и ранжирование из main.py / ranking.py без изменений и
добавляет к ним:
- Recommender DB (PostgreSQL) — источник истины для событий и весов;
- потребителя событий из брокера (Redis Stream) с дедупликацией по event_id.

Хранилище в памяти стартует пустым (empty_data.json вместо тестовых данных)
и заполняется из Recommender DB.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

os.environ.setdefault(
    "RECOMMENDER_DATA_FILE", str(Path(__file__).resolve().parent / "empty_data.json")
)

from main import app, store  # noqa: E402
from consumer import EventConsumer  # noqa: E402
from pg_store import RecommenderDb  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("recommender")

RECOMMENDER_DB_DSN = os.getenv("RECOMMENDER_DB_DSN", "postgresql://rec:rec@localhost:5440/rec")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_state: dict = {}


async def _connect_db(attempts: int = 30, delay: float = 2.0) -> RecommenderDb:
    for attempt in range(1, attempts + 1):
        try:
            db = await asyncio.to_thread(RecommenderDb, RECOMMENDER_DB_DSN)
            await asyncio.to_thread(db.init_schema)
            return db
        except Exception as exc:
            log.warning("Recommender DB недоступна (попытка %d): %s", attempt, exc)
            await asyncio.sleep(delay)
    raise RuntimeError("Recommender DB так и не стала доступна")


async def _startup() -> None:
    import redis.asyncio as aioredis

    db = await _connect_db()
    await asyncio.to_thread(db.load_into, store)
    redis_client = aioredis.Redis.from_url(REDIS_URL)
    consumer = EventConsumer(redis_client, db, store)
    stop = asyncio.Event()
    _state.update(db=db, redis=redis_client, consumer=consumer, stop=stop,
                  task=asyncio.create_task(consumer.run(stop)))


async def _shutdown() -> None:
    if "stop" in _state:
        _state["stop"].set()
        _state["task"].cancel()
        await _state["redis"].aclose()


app.add_event_handler("startup", _startup)
app.add_event_handler("shutdown", _shutdown)


@app.get("/internal/consumer")
async def consumer_stats():
    """Отладка: сколько событий применено, отброшено как дубли, с ошибкой."""
    consumer = _state.get("consumer")
    return consumer.stats if consumer else {"status": "not started"}
