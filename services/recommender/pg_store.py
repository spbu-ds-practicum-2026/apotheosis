"""Recommender DB (PostgreSQL): применение событий с дедупликацией по event_id.

Правило из ТР: в одной транзакции вставить event_id в processed_events
(INSERT ... ON CONFLICT DO NOTHING) и, только если вставка произошла,
обновить историю взаимодействий и веса интересов. Подтверждение (ack)
брокеру отправляется после коммита этой транзакции.

Ранжирование (ranking.py) работает с RecommenderStore в памяти; он
заполняется из Recommender DB при старте и обновляется после каждого
закоммиченного события, поэтому после перезапуска состояние не теряется.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, ContextManager

from schemas import Event, PinMetadata
from store import LIKE_WEIGHT, VIEW_WEIGHT, RecommenderStore

log = logging.getLogger("recommender.db")

SCHEMA_FILE = Path(__file__).resolve().parent / "db" / "schema.sql"

# event_type из формата брокера (docs/api.md) -> type в schemas.Event
EVENT_TYPES = {"pin_created": "pin_created", "pin_liked": "like", "pin_viewed": "view"}


class BadEvent(ValueError):
    pass


def parse_broker_event(raw) -> Event:
    """Событие из брокера в формате docs/api.md -> schemas.Event."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
        payload = data.get("payload") or {}
        kind = EVENT_TYPES[data["event_type"]]
        ts = data.get("timestamp")
        return Event(
            event_id=str(data["event_id"]),
            type=kind,
            user_id=data["user_id"],
            pin_id=str(data["pin_id"]),
            author_id=payload.get("author_id") or data.get("author_id") or data["user_id"],
            tags=list(payload.get("tags") or []) if kind == "pin_created" else [],
            occurred_at=datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BadEvent(f"некорректное событие: {exc}") from None


def _psycopg_connect_factory(dsn: str) -> Callable[[], ContextManager]:
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(dsn, min_size=1, max_size=5, kwargs={"row_factory": dict_row}, open=True)
    return pool.connection


class RecommenderDb:
    def __init__(self, dsn: str, connect: Callable[[str], ContextManager] | None = None):
        self._dsn = dsn
        if connect is not None:
            self._connect = lambda: connect(dsn)
        else:
            self._connect = _psycopg_connect_factory(dsn)

    @contextmanager
    def tx(self):
        with self._connect() as conn:
            yield conn

    def init_schema(self) -> None:
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        with self.tx() as conn:
            for stmt in (s.strip() for s in sql.split(";")):
                if stmt and not all(l.strip().startswith("--") for l in stmt.splitlines() if l.strip()):
                    conn.execute(stmt)

    def apply(self, event: Event) -> bool:
        """True — событие применено; False — дубль (event_id уже обработан)."""
        with self.tx() as conn:
            fresh = conn.execute(
                "INSERT INTO processed_events (event_id, event_type) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING RETURNING event_id",
                (event.event_id, event.type),
            ).fetchone()
            if fresh is None:
                return False

            if event.type == "pin_created":
                conn.execute(
                    "INSERT INTO pins (pin_id, author_id, tags, created_at) "
                    "VALUES (%s, %s, %s::text[], %s::timestamptz) ON CONFLICT DO NOTHING",
                    (event.pin_id, event.author_id, list(event.tags), event.occurred_at),
                )
                conn.execute(
                    "INSERT INTO pin_stats (pin_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (event.pin_id,),
                )
                return True

            conn.execute(
                "INSERT INTO interactions (event_id, user_id, pin_id, kind, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s::timestamptz)",
                (event.event_id, event.user_id, event.pin_id, event.type, event.occurred_at),
            )
            if event.type == "like":
                first_like = conn.execute(
                    "INSERT INTO user_likes (user_id, pin_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING pin_id",
                    (event.user_id, event.pin_id),
                ).fetchone()
                if first_like is None:
                    return True  # повторный лайк того же пина вес не удваивает
                conn.execute(
                    "INSERT INTO pin_stats (pin_id, likes) VALUES (%s, 1) "
                    "ON CONFLICT (pin_id) DO UPDATE SET likes = pin_stats.likes + 1",
                    (event.pin_id,),
                )
                weight = LIKE_WEIGHT
            else:
                conn.execute(
                    "INSERT INTO pin_stats (pin_id, views) VALUES (%s, 1) "
                    "ON CONFLICT (pin_id) DO UPDATE SET views = pin_stats.views + 1",
                    (event.pin_id,),
                )
                weight = VIEW_WEIGHT

            conn.execute(
                "INSERT INTO interests (user_id, tag, weight) "
                "SELECT %s::text, t, %s::double precision FROM pins, unnest(pins.tags) AS t "
                "WHERE pins.pin_id = %s "
                "ON CONFLICT (user_id, tag) DO UPDATE SET weight = interests.weight + EXCLUDED.weight",
                (event.user_id, weight, event.pin_id),
            )
            return True

    def load_into(self, store: RecommenderStore) -> None:
        """Заполнить RecommenderStore в памяти из Recommender DB."""
        with self.tx() as conn:
            pins = conn.execute("SELECT pin_id, author_id, tags, created_at FROM pins").fetchall()
            processed = conn.execute("SELECT event_id FROM processed_events").fetchall()
            interests = conn.execute("SELECT user_id, tag, weight FROM interests").fetchall()
            likes = conn.execute("SELECT user_id, pin_id FROM user_likes").fetchall()
            stats = conn.execute("SELECT pin_id, likes, views FROM pin_stats").fetchall()
        for p in pins:
            store.add_pin(PinMetadata(
                pin_id=p["pin_id"], author_id=p["author_id"], image_key="", title="",
                tags=list(p["tags"] or []), created_at=p["created_at"],
            ))
        store.processed_events.update(r["event_id"] for r in processed)
        for r in interests:
            store.interests[r["user_id"]][r["tag"]] = float(r["weight"])
        for r in likes:
            store.liked[r["user_id"]].add(r["pin_id"])
        for r in stats:
            if r["likes"]:
                store.like_count[r["pin_id"]] = int(r["likes"])
            if r["views"]:
                store.view_count[r["pin_id"]] = int(r["views"])
        log.info("загружено из Recommender DB: пинов %d, событий %d", len(pins), len(processed))
