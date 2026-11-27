"""Утилиты для демонстрации распределённых гарантий.

    python -m server.demo_tools consistency --rec-dsn postgresql://rec:rec@recommender-db:5432/rec
        Сравнить число лайков в Feed DB (все узлы кольца) и в Recommender DB и показать,
        сколько событий ещё в outbox. После прекращения операций числа сходятся
        (eventual consistency).

    python -m server.demo_tools replay 5
        Повторно отправить в брокер последние 5 событий (имитация повторной доставки).
        Recommender отбросит их по event_id: GET :8002/internal/consumer -> duplicates.

    python -m server.demo_tools where <логин>
        На каком узле кольца лежат данные пользователя и кто мастер этого узла.
"""
from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

import feed.db.connection as fdb
from feed.db.connection import get_conn_by_dsn

from server import config
from server.topology import Topology


def _feed_totals() -> tuple[int, int, list[str]]:
    likes = unsent = 0
    down = []
    ring, dsns = fdb.current_topology()
    for node in ring:
        try:
            with get_conn_by_dsn(dsns[node]) as c:
                likes += c.execute("SELECT count(*) AS n FROM interactions WHERE kind = 'like'").fetchone()["n"]
                unsent += c.execute("SELECT count(*) AS n FROM outbox WHERE sent_at IS NULL").fetchone()["n"]
        except Exception as exc:
            down.append(f"{node} ({type(exc).__name__})")
    return likes, unsent, down


def consistency(rec_dsn: str) -> int:
    likes, unsent, down = _feed_totals()
    with get_conn_by_dsn(rec_dsn) as c:
        rec_likes = c.execute("SELECT coalesce(sum(likes), 0) AS n FROM pin_stats").fetchone()["n"]
        processed = c.execute("SELECT count(*) AS n FROM processed_events").fetchone()["n"]
    print(f"Feed DB:        лайков {likes}, событий ещё в outbox {unsent}"
          + (f", недоступны: {', '.join(down)}" if down else ""))
    print(f"Recommender DB: лайков {rec_likes}, обработано событий {processed}")
    print("сошлось" if likes == rec_likes and unsent == 0 else "ещё не сошлось (события в пути)")
    return 0


def replay(n: int) -> int:
    import redis

    r = redis.Redis.from_url(config.REDIS_URL)
    msgs = r.xrevrange(config.EVENTS_STREAM, count=n)
    for _id, fields in reversed(msgs):
        r.xadd(config.EVENTS_STREAM, fields)
    print(f"повторно отправлено событий: {len(msgs)}")
    return 0


def where(user: str) -> int:
    node = fdb.node_for(user)
    dsn = fdb.dsn_for_node(node)
    u = urlparse(dsn)
    print(f"{user}: узел {node}, мастер {u.hostname}:{u.port}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("consistency")
    c.add_argument("--rec-dsn", default="postgresql://rec:rec@recommender-db:5432/rec")
    r = sub.add_parser("replay")
    r.add_argument("n", type=int, nargs="?", default=5)
    w = sub.add_parser("where")
    w.add_argument("user")
    args = p.parse_args(argv)
    topo = Topology.from_env()
    topo.wait_ready(30)
    if args.cmd == "consistency":
        return consistency(args.rec_dsn)
    if args.cmd == "replay":
        return replay(args.n)
    return where(args.user)


if __name__ == "__main__":
    sys.exit(main())
