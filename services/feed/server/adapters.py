"""Адаптеры, через которые FeedService (feed_service.py) работает с Feed DB и Redis.

FeedService ожидает:
- хранилище пинов с get_many([(author_id, pin_id)]) и recent(limit);
- кэш с get / set(key, value, ttl) / delete_prefix(prefix).
"""
from __future__ import annotations

import logging
import pickle
from typing import Any, Optional

from cache import TTLCache
from schemas import PinMetadata

from server import config, repository

log = logging.getLogger("feed.adapters")


class DbPinStore:
    """Пины из шардов Feed DB (ключ шарда — author_id)."""

    def get_many(self, items: list[tuple[str, str]]) -> list[PinMetadata]:
        found = repository.get_pins(items)
        # порядок задан ранжированием; пропавшие и недоступные пины пропускаются
        return [PinMetadata(**found[pid]) for _, pid in items if pid in found]

    def recent(self, limit: int) -> list[PinMetadata]:
        return [PinMetadata(**p) for p in repository.recent_pins(limit)]


class RedisCache:
    """Кэш в Redis с запасным локальным кэшем на время недоступности Redis.

    Ключи: {CACHE_PREFIX}<ключ FeedService>, например feedsvc:portion:alice:...
    """

    def __init__(self, client=None, url: str = config.REDIS_URL, prefix: str = config.CACHE_PREFIX):
        self._client = client
        self._url = url
        self._prefix = prefix
        self._local = TTLCache()

    def _redis(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self._url, socket_timeout=0.3, socket_connect_timeout=0.3
            )
        return self._client

    def get(self, key: str) -> Optional[Any]:
        try:
            raw = self._redis().get(self._prefix + key)
        except Exception:
            log.warning("Redis недоступен (get), локальный кэш")
            return self._local.get(key)
        return None if raw is None else pickle.loads(raw)

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._local.set(key, value, ttl)
        try:
            self._redis().set(self._prefix + key, pickle.dumps(value), ex=max(1, int(ttl)))
        except Exception:
            log.warning("Redis недоступен (set), только локальный кэш")

    def delete_prefix(self, prefix: str) -> None:
        self._local.delete_prefix(prefix)
        try:
            r = self._redis()
            keys = list(r.scan_iter(match=self._prefix + prefix + "*", count=500))
            if keys:
                r.delete(*keys)
        except Exception:
            log.warning("Redis недоступен (delete_prefix)")


class CursorMap:
    """Gateway передаёт cursor как целое число (номер порции), а FeedService и
    Recommender работают с непрозрачными строками. Номер порции n хранится
    в соответствии с настоящим курсором: {prefix}cursor:<userId>:<n>."""

    def __init__(self, cache: RedisCache, ttl: int = config.CURSOR_TTL_SECONDS):
        self._cache = cache
        self._ttl = ttl

    def resolve(self, user_id: str, n: int) -> tuple[Optional[str], bool]:
        """(настоящий курсор, найден ли он). n = 0 — начало ленты."""
        if n == 0:
            return None, True
        value = self._cache.get(f"cursor:{user_id}:{n}")
        if value is None:
            return None, False
        return value or None, True

    def remember(self, user_id: str, n: int, real_cursor: Optional[str]) -> None:
        self._cache.set(f"cursor:{user_id}:{n}", real_cursor or "", self._ttl)
