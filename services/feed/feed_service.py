import asyncio
import logging
import time
from typing import Optional, Protocol

from schemas import FeedResponse, PinMetadata, RecommendationResponse, RecommendedItem
from cache import TTLCache

log = logging.getLogger("feed")

RECOMMENDER_TIMEOUT_S = 0.5
PORTION_TTL_S = 60
FALLBACK_TTL_S = 10
POPULAR_TTL_S = 300
FALLBACK_CURSOR_PREFIX = "fb:"


class RecommenderClient(Protocol):
    async def recommendations(self, user_id: str, cursor: Optional[str], limit: int) -> RecommendationResponse: ...
    async def popular(self, limit: int) -> list[RecommendedItem]: ...


class PinStore(Protocol):
    def get_many(self, items: list[tuple[str, str]]) -> list[PinMetadata]: ...
    def recent(self, limit: int) -> list[PinMetadata]: ...


class CircuitBreaker:

    def __init__(self, failure_threshold: int = 3, reset_after: float = 10.0, clock=time.monotonic):
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self.clock = clock
        self.failures = 0
        self.opened_at: Optional[float] = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        return self.clock() - self.opened_at >= self.reset_after

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold or self.opened_at is not None:
            self.opened_at = self.clock()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self.allow() else "open"


class FeedService:
    def __init__(self, recommender: RecommenderClient, pins: PinStore,
                 cache: Optional[TTLCache] = None, breaker: Optional[CircuitBreaker] = None):
        self.recommender = recommender
        self.pins = pins
        self.cache = cache or TTLCache()
        self.breaker = breaker or CircuitBreaker()

    async def get_portion(self, user_id: str, cursor: Optional[str], limit: int) -> FeedResponse:
        key = self._portion_key(user_id, cursor, limit)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        if cursor and cursor.startswith(FALLBACK_CURSOR_PREFIX):
            # клиент листает запасную выдачу; когда Recommender оживёт,
            # следующая прокрутка с начала снова будет персональной
            return await self._fallback(user_id, cursor, limit)

        rec = await self._call_recommender(user_id, cursor, limit)
        if rec is None:
            return await self._fallback(user_id, None, limit)

        portion = FeedResponse(
            pins=self.pins.get_many([(it.author_id, it.pin_id) for it in rec.items]),
            next_cursor=rec.next_cursor,
            source="recommender",
        )
        self.cache.set(key, portion, PORTION_TTL_S)
        return portion

    def invalidate_user(self, user_id: str) -> None:
        self.cache.delete_prefix(f"portion:{user_id}:")

    async def _call_recommender(self, user_id: str, cursor: Optional[str], limit: int) -> Optional[RecommendationResponse]:
        if not self.breaker.allow():
            return None
        try:
            rec = await asyncio.wait_for(
                self.recommender.recommendations(user_id, cursor, limit), RECOMMENDER_TIMEOUT_S)
        except Exception as e:  # таймаут, сеть, 5xx
            self.breaker.failure()
            log.warning("recommender failed (%s), breaker=%s", type(e).__name__, self.breaker.state)
            return None
        self.breaker.success()
        await self._refresh_popular_if_stale()
        return rec

    async def _fallback(self, user_id: str, cursor: Optional[str], limit: int) -> FeedResponse:
        offset = 0
        if cursor and cursor.startswith(FALLBACK_CURSOR_PREFIX):
            tail = cursor[len(FALLBACK_CURSOR_PREFIX):]
            offset = int(tail) if tail.isdigit() else 0

        popular: Optional[list[RecommendedItem]] = self.cache.get("popular")
        if popular:
            pool = self.pins.get_many([(it.author_id, it.pin_id) for it in popular])
            source = "popular"
        else:
            pool = self.pins.recent(200)   # scatter-gather по всем шардам
            source = "recent"

        if not pool:
            return FeedResponse(pins=[], next_cursor=None, source=source)
        offset %= len(pool)   # бесконечный скролл по кругу
        page = (pool[offset:] + pool[:offset])[:limit]
        portion = FeedResponse(pins=page, next_cursor=f"{FALLBACK_CURSOR_PREFIX}{offset + len(page)}", source=source)
        self.cache.set(self._portion_key(user_id, cursor, limit), portion, FALLBACK_TTL_S)
        return portion

    async def _refresh_popular_if_stale(self) -> None:
        if self.cache.get("popular") is not None:
            return
        try:
            items = await asyncio.wait_for(self.recommender.popular(100), RECOMMENDER_TIMEOUT_S)
            self.cache.set("popular", items, POPULAR_TTL_S)
        except Exception as e:
            log.warning("popular refresh failed: %s", type(e).__name__)

    @staticmethod
    def _portion_key(user_id: str, cursor: Optional[str], limit: int) -> str:
        return f"portion:{user_id}:{cursor or 'start'}:{limit}"
