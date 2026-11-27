"""Потребитель событий из брокера (Redis Stream, consumer group).

Порядок для каждого сообщения:
1. Транзакция Recommender DB: processed_events + история + веса (pg_store.apply).
2. Коммит -> то же событие применяется к RecommenderStore в памяти.
3. XACK брокеру. Если процесс упал между коммитом и ack, сообщение
   останется в pending и придёт снова: processed_events отбросит дубль.
4. Для лайка и просмотра удаляются кэшированные порции ленты пользователя
   в Redis, чтобы следующая порция строилась с новыми весами.

Сообщения, не дошедшие до ack (сбой БД, падение процесса), перечитываются:
свои — из pending при каждом проходе, брошенные упавшими экземплярами —
через XAUTOCLAIM после CLAIM_IDLE_MS простоя.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

from pg_store import BadEvent, RecommenderDb, parse_broker_event
from store import RecommenderStore

log = logging.getLogger("recommender.consumer")

STREAM = os.getenv("EVENTS_STREAM", "events")
GROUP = os.getenv("EVENTS_GROUP", "recommender")
CONSUMER = os.getenv("EVENTS_CONSUMER", socket.gethostname())
FEED_CACHE_PREFIX = os.getenv("FEED_CACHE_PREFIX", "feedsvc:")
CLAIM_IDLE_MS = int(os.getenv("CLAIM_IDLE_MS", "60000"))
BATCH = int(os.getenv("EVENTS_BATCH", "100"))
BLOCK_MS = int(os.getenv("EVENTS_BLOCK_MS", "2000"))


def _s(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class EventConsumer:
    def __init__(self, redis_client, db: RecommenderDb, store: RecommenderStore,
                 consumer: str = CONSUMER):
        self.redis = redis_client          # redis.asyncio.Redis
        self.db = db
        self.store = store
        self.consumer = consumer
        self.stats = {"applied": 0, "duplicates": 0, "bad": 0, "failed": 0}

    async def ensure_group(self) -> None:
        try:
            # id "0": события, накопленные до первого запуска, тоже будут обработаны
            await self.redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def handle(self, msg_id, fields) -> bool:
        """Обработать одно сообщение. True — можно делать ack."""
        raw = None
        for k, v in fields.items():
            if _s(k) == "event":
                raw = _s(v)
        try:
            event = parse_broker_event(raw)
        except BadEvent:
            log.error("сообщение %s отброшено: %r", _s(msg_id), raw)
            self.stats["bad"] += 1
            return True
        try:
            applied = await asyncio.to_thread(self.db.apply, event)
        except Exception:
            log.warning("событие %s не применено, повторим", event.event_id, exc_info=True)
            self.stats["failed"] += 1
            return False
        if applied:
            self.store.apply(event)   # в цикле событий, как и HTTP-обработчики ranking
            self.stats["applied"] += 1
            if event.type in ("like", "view"):
                await self.invalidate_feed(event.user_id)
        else:
            self.stats["duplicates"] += 1
        return True

    async def invalidate_feed(self, user_id: str) -> None:
        try:
            keys = [k async for k in self.redis.scan_iter(
                match=f"{FEED_CACHE_PREFIX}portion:{user_id}:*", count=500)]
            if keys:
                await self.redis.delete(*keys)
        except Exception:
            log.warning("не удалось сбросить кэш ленты %s", user_id, exc_info=True)

    async def _process(self, messages) -> bool:
        """True, если все сообщения подтверждены."""
        all_ok = True
        for msg_id, fields in messages:
            if fields is None:      # сообщение удалено из потока (MAXLEN), подтверждаем
                await self.redis.xack(STREAM, GROUP, msg_id)
                continue
            if await self.handle(msg_id, fields):
                await self.redis.xack(STREAM, GROUP, msg_id)
            else:
                all_ok = False
                break               # порядок событий сохраняем: дальше не идём
        return all_ok

    async def poll_once(self, block_ms: int = BLOCK_MS) -> int:
        """Один проход: свои pending, брошенные чужие, новые. Возвращает число сообщений."""
        total = 0
        pending = await self.redis.xreadgroup(GROUP, self.consumer, {STREAM: "0"}, count=BATCH)
        for _stream, messages in pending or []:
            total += len(messages)
            if not await self._process(messages):
                await asyncio.sleep(1)
                return total

        claimed = await self.redis.xautoclaim(STREAM, GROUP, self.consumer, CLAIM_IDLE_MS,
                                              start_id="0", count=BATCH)
        if claimed and claimed[1]:
            total += len(claimed[1])
            if not await self._process(claimed[1]):
                await asyncio.sleep(1)
                return total

        fresh = await self.redis.xreadgroup(GROUP, self.consumer, {STREAM: ">"},
                                            count=BATCH, block=block_ms)
        for _stream, messages in fresh or []:
            total += len(messages)
            if not await self._process(messages):
                await asyncio.sleep(1)
        return total

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.ensure_group()
                break
            except Exception:
                log.warning("брокер недоступен, ждём", exc_info=True)
                await asyncio.sleep(2)
        while not stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if "NOGROUP" in str(exc):
                    await self.ensure_group()
                log.warning("ошибка чтения из брокера", exc_info=True)
                await asyncio.sleep(1)
