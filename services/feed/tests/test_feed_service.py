import asyncio

import pytest

from schemas import RecommendationResponse, RecommendedItem
from cache import TTLCache
from feed_service import CircuitBreaker, FeedService
from pin_store import InMemoryPinStore


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeRecommender:
    """Подменяет Recommender: пины из test_pins.json по порядку, курсор — смещение."""

    def __init__(self):
        self.pins = InMemoryPinStore.from_file().recent(1000)
        self.mode = "ok"        # ok | slow | error
        self.calls = 0

    async def recommendations(self, user_id, cursor, limit):
        self.calls += 1
        await self._maybe_fail()
        offset = int(cursor) if cursor else 0
        page = self.pins[offset:offset + limit]
        items = [RecommendedItem(pin_id=p.pin_id, author_id=p.author_id, score=1.0) for p in page]
        return RecommendationResponse(items=items, next_cursor=str(offset + len(page)))

    async def popular(self, limit):
        await self._maybe_fail()
        return [RecommendedItem(pin_id=p.pin_id, author_id=p.author_id, score=0.0) for p in self.pins[:limit]]

    async def _maybe_fail(self):
        if self.mode == "slow":
            await asyncio.sleep(2)
        if self.mode == "error":
            raise ConnectionError("down")


@pytest.fixture
def env():
    clock = Clock()
    rec = FakeRecommender()
    svc = FeedService(rec, InMemoryPinStore.from_file(), cache=TTLCache(clock),
                      breaker=CircuitBreaker(failure_threshold=3, reset_after=10, clock=clock))
    return svc, rec, clock


def run(coro):
    return asyncio.run(coro)


def test_personal_portion(env):
    svc, rec, _ = env
    p = run(svc.get_portion("alice", None, 10))
    assert p.source == "recommender" and len(p.pins) == 10 and p.next_cursor


def test_scroll_continues_with_cursor(env):
    svc, _, _ = env
    p1 = run(svc.get_portion("bob", None, 10))
    p2 = run(svc.get_portion("bob", p1.next_cursor, 10))
    assert not {p.pin_id for p in p1.pins} & {p.pin_id for p in p2.pins}


def test_portion_cached_and_expires(env):
    svc, rec, clock = env
    run(svc.get_portion("alice", None, 10))
    run(svc.get_portion("alice", None, 10))
    assert rec.calls == 1
    clock.t += 61
    run(svc.get_portion("alice", None, 10))
    assert rec.calls == 2


def test_invalidate_user(env):
    svc, rec, _ = env
    run(svc.get_portion("alice", None, 10))
    svc.invalidate_user("alice")
    run(svc.get_portion("alice", None, 10))
    assert rec.calls == 2


def test_timeout_falls_back_to_popular(env):
    svc, rec, _ = env
    run(svc.get_portion("warmup", None, 5))      # успешный вызов прогревает кэш популярного
    rec.mode = "slow"
    p = run(svc.get_portion("alice", None, 10))
    assert p.source == "popular" and len(p.pins) == 10
    assert p.next_cursor.startswith("fb:")


def test_fallback_to_recent_without_popular_cache(env):
    svc, rec, _ = env
    rec.mode = "error"
    p = run(svc.get_portion("alice", None, 10))
    assert p.source == "recent" and len(p.pins) == 10


def test_fallback_scroll_is_infinite(env):
    svc, rec, _ = env
    rec.mode = "error"
    cursor, total = None, 0
    for _ in range(8):   # 80 > 44 пинов — идём по кругу
        p = run(svc.get_portion("alice", cursor, 10))
        assert len(p.pins) == 10
        cursor, total = p.next_cursor, total + len(p.pins)
    assert total == 80


def test_circuit_breaker_opens_and_recovers(env):
    svc, rec, clock = env
    rec.mode = "error"
    for i in range(3):
        run(svc.get_portion(f"u{i}", None, 5))
    assert svc.breaker.state == "open"
    calls = rec.calls
    p = run(svc.get_portion("alice", None, 5))
    assert rec.calls == calls and p.source != "recommender"   # не ждём таймаут, сразу запасной путь

    rec.mode = "ok"
    clock.t += 11                                             # half-open: одна пробная попытка
    p = run(svc.get_portion("bob", None, 5))
    assert p.source == "recommender" and svc.breaker.state == "closed"


def test_fallback_portion_cached_10s(env):
    svc, rec, clock = env
    rec.mode = "error"
    run(svc.get_portion("alice", None, 5))
    rec.mode = "ok"
    assert run(svc.get_portion("alice", None, 5)).source != "recommender"
    clock.t += 11
    assert run(svc.get_portion("alice", None, 5)).source == "recommender"
