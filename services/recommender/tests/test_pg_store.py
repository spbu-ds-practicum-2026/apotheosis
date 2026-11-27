"""Recommender DB: дедупликация по event_id и обновление весов.

Нужна пустая база PostgreSQL: REC_DB_DSN=postgresql://... pytest tests/test_pg_store.py
Без REC_DB_DSN тесты пропускаются. Если psycopg не установлен, используется
тестовый клиент services/feed/tests/pgwire.py.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pg_store import BadEvent, RecommenderDb, parse_broker_event
from ranking import rank
from store import RecommenderStore

DSN = os.environ.get("REC_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="задайте REC_DB_DSN")


def _connect():
    try:
        import psycopg  # noqa: F401
        return None
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "feed" / "tests"))
        import pgwire
        return pgwire.connection


@pytest.fixture
def db():
    d = RecommenderDb(DSN, connect=_connect())
    d.init_schema()
    with d.tx() as conn:
        conn.execute("TRUNCATE interactions, processed_events, pins, user_likes, interests, pin_stats")
    return d


def ev(event_type, user, pin, author="author", tags=None, event_id=None):
    return parse_broker_event({
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "user_id": user,
        "pin_id": pin,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"author_id": author, "shard": "N1", "tags": tags or []},
    })


def test_parse_broker_event():
    e = ev("pin_liked", "bob", "p1", author="alice", tags=["горы"])
    assert (e.type, e.user_id, e.pin_id, e.author_id, e.tags) == ("like", "bob", "p1", "alice", [])
    with pytest.raises(BadEvent):
        parse_broker_event('{"event_type": "unknown"}')


def test_duplicate_event_not_applied_twice(db):
    db.apply(ev("pin_created", "alice", "p1", "alice", ["горы", "закат"]))
    like = ev("pin_liked", "bob", "p1", "alice")
    assert db.apply(like) is True
    assert db.apply(like) is False                       # повторная доставка
    assert db.apply(ev("pin_liked", "bob", "p1", "alice")) is True   # новый event_id, тот же лайк
    with db.tx() as conn:
        w = {r["tag"]: r["weight"] for r in conn.execute(
            "SELECT tag, weight FROM interests WHERE user_id = 'bob'").fetchall()}
        stats = conn.execute("SELECT likes, views FROM pin_stats WHERE pin_id = 'p1'").fetchone()
        hist = conn.execute("SELECT count(*) AS n FROM interactions").fetchone()["n"]
    assert w == {"горы": 1.0, "закат": 1.0}                # вес не удвоился
    assert (stats["likes"], stats["views"]) == (1, 0)
    assert hist == 2                                      # оба разных события в истории


def test_views_add_small_weight(db):
    db.apply(ev("pin_created", "alice", "p1", "alice", ["море"]))
    for _ in range(3):
        db.apply(ev("pin_viewed", "bob", "p1", "alice"))
    with db.tx() as conn:
        w = conn.execute("SELECT weight FROM interests WHERE user_id='bob' AND tag='море'").fetchone()
    assert abs(w["weight"] - 0.3) < 1e-9


def test_load_into_store_matches_in_memory_apply(db):
    events = [
        ev("pin_created", "alice", "p1", "alice", ["горы"]),
        ev("pin_created", "carol", "p2", "carol", ["море"]),
        ev("pin_created", "dave", "p3", "dave", ["горы", "снег"]),
    ]
    events += [ev("pin_liked", "bob", "p1", "alice"), ev("pin_viewed", "bob", "p2", "carol")]
    live = RecommenderStore()
    for e in events:
        if db.apply(e):
            live.apply(e)

    restored = RecommenderStore()
    db.load_into(restored)
    assert restored.user_interests("bob") == pytest.approx(live.user_interests("bob"))
    assert restored.liked["bob"] == {"p1"}
    assert restored.processed_events >= {e.event_id for e in events}
    # ранжирование после перезапуска то же: горы впереди моря
    assert [s.pin_id for s in rank(restored, "bob")] == [s.pin_id for s in rank(live, "bob")]
    assert rank(restored, "bob")[0].pin_id == "p3"
