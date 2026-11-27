from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

POPULAR_KEY = "feed:popular"
PORTION_TTL_SECONDS = 60


def portion_key(user_id: str, portion: int) -> str:
    return f"feed:{user_id}:{portion}"


class Cache:
    def __init__(self, client=None, url: str | None = None) -> None:
        self._client = client
        self._url = url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    def _redis(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self._url, socket_timeout=0.3, socket_connect_timeout=0.3, decode_responses=True
            )
        return self._client

    def get_json(self, key: str):
        try:
            raw = self._redis().get(key)
            return None if raw is None else json.loads(raw)
        except Exception:
            log.warning("Redis: чтение %s не удалось", key, exc_info=True)
            return None

    def set_json(self, key: str, value, ttl_seconds: int = PORTION_TTL_SECONDS) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds должен быть > 0")
        try:
            self._redis().set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
            return True
        except Exception:
            log.warning("Redis: запись %s не удалась", key, exc_info=True)
            return False

    def delete(self, *keys: str) -> bool:
        try:
            if keys:
                self._redis().delete(*keys)
            return True
        except Exception:
            log.warning("Redis: удаление не удалось", exc_info=True)
            return False

    def invalidate_user(self, user_id: str) -> bool:
        try:
            keys = list(self._redis().scan_iter(match=f"feed:{user_id}:*"))
            if keys:
                self._redis().delete(*keys)
            return True
        except Exception:
            log.warning("Redis: инвалидация %s не удалась", user_id, exc_info=True)
            return False
