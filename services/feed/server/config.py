import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RECOMMENDER_URL = os.getenv("RECOMMENDER_URL", "http://localhost:8002")

# Брокер сообщений: Redis Stream, в который relay пересылает события из outbox.
EVENTS_STREAM = os.getenv("EVENTS_STREAM", "events")
EVENTS_STREAM_MAXLEN = _int("EVENTS_STREAM_MAXLEN", 100_000)

RELAY_INTERVAL_SECONDS = float(os.getenv("RELAY_INTERVAL_SECONDS", "0.5"))
RELAY_BATCH = _int("RELAY_BATCH", 100)

# Префикс ключей кэша порций в Redis. Recommender удаляет
# {CACHE_PREFIX}portion:{userId}:* после применения события пользователя.
CACHE_PREFIX = os.getenv("FEED_CACHE_PREFIX", "feedsvc:")
CURSOR_TTL_SECONDS = _int("CURSOR_TTL_SECONDS", 30 * 60)

# Запись идемпотентности создания пина в статусе in_progress моложе этого
# считается «запрос обрабатывается» (ТР, сценарий добавления пина, шаг 5).
PIN_REQUEST_STALE_SECONDS = _int("PIN_REQUEST_STALE_SECONDS", 30)

SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"
