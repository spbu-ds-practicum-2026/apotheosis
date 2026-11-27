"""Отправитель outbox: события из outbox всех узлов Feed DB -> Redis Stream.

Семантика at-least-once. На каждом узле в одной транзакции:
выбрать неотправленные строки (FOR UPDATE SKIP LOCKED, чтобы несколько
инстансов Feed не отправляли одно и то же одновременно), XADD каждой в поток,
отметить sent_at. Если XADD или коммит не прошли, транзакция откатывается и
строки отправляются снова при следующем проходе: брокер может получить дубль,
Recommender отбрасывает его по event_id.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from feed.db.connection import all_node_dsns, get_conn_by_dsn

from server import config

log = logging.getLogger("feed.relay")


def relay_once(redis_client, dsns: list[str] | None = None, batch: int = config.RELAY_BATCH) -> int:
    """Один проход по всем узлам. Возвращает число отправленных событий."""
    sent = 0
    for dsn in dsns if dsns is not None else all_node_dsns():
        try:
            with get_conn_by_dsn(dsn) as conn:
                rows = conn.execute(
                    "SELECT event_id, payload FROM outbox WHERE sent_at IS NULL "
                    "ORDER BY created_at, event_id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (batch,),
                ).fetchall()
                if not rows:
                    continue
                for row in rows:
                    payload = row["payload"]
                    if not isinstance(payload, str):
                        payload = json.dumps(payload, ensure_ascii=False)
                    redis_client.xadd(
                        config.EVENTS_STREAM, {"event": payload},
                        maxlen=config.EVENTS_STREAM_MAXLEN, approximate=True,
                    )
                conn.execute(
                    "UPDATE outbox SET sent_at = now() WHERE event_id = ANY(%s::uuid[])",
                    ([str(r["event_id"]) for r in rows],),
                )
                sent += len(rows)
        except Exception:
            log.warning("outbox: проход по узлу не удался, повторим позже", exc_info=True)
    return sent


class OutboxRelay:
    def __init__(self, redis_client=None, interval: float = config.RELAY_INTERVAL_SECONDS):
        self._redis = redis_client
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _client(self):
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(config.REDIS_URL, socket_timeout=2)
        return self._redis

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="outbox-relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                n = relay_once(self._client())
            except Exception:
                log.warning("outbox relay: ошибка", exc_info=True)
                n = 0
            if n == 0:
                self._stop.wait(self._interval)
