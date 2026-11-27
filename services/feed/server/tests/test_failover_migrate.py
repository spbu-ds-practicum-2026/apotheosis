"""Отказ мастера узла (failover) и смена состава кольца (перенос узла) на настоящем PostgreSQL.

Patroni здесь имитируется: настоящая потоковая синхронная репликация PostgreSQL,
повышение реплики через pg_promote() и ключ лидера в поддельном etcd (fake_etcd.py),
в том же формате, что пишет Patroni. Проверяется то, что спроектировано в Feed:
чтение топологии из etcd, повтор записи при отказе и онлайн-перенос ключей.

Окружение:
  FEED_E2E=1, FEED_NODES=N1,N2,N3, FEED_DSN_N1..N3 — пустые базы со схемой;
  FEED_DSN_N4 — пустая база со схемой (для переноса);
  FEED_FAILOVER_PRIMARY / FEED_FAILOVER_STANDBY — DSN мастера и синхронной реплики
  одного кластера (база feed со схемой); FEED_FAILOVER_STOP — команда, которая
  «роняет» мастер (например, pg_ctl -D ... -m immediate stop).
Тест failover разрушающий: после него мастером становится бывшая реплика.
"""
import json
import os
import random
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

import pytest

import feed.db.connection as fdb
from feed.db.connection import get_conn, get_conn_by_dsn
from server import failover, migrate, repository as repo, topology as topo_mod
from server.tests.conftest import FakeStorage
from server.tests.fake_etcd import FakeEtcd


def _conn_url(dsn: str) -> str:
    u = urlparse(dsn)
    return f"postgres://{u.hostname}:{u.port}/{u.path.lstrip('/')}"


def _register_member(etcd: FakeEtcd, scope: str, name: str, dsn: str, leader: bool):
    etcd.set(f"/service/{scope}/members/{name}", json.dumps({"conn_url": _conn_url(dsn), "role": "master" if leader else "replica"}))
    if leader:
        etcd.set(f"/service/{scope}/leader", name)


@pytest.fixture
def etcd(db, monkeypatch):
    e = FakeEtcd()
    nodes = os.environ["FEED_NODES"].split(",")
    for n in nodes:
        _register_member(e, f"feed-{n.lower()}", f"{n.lower()}a", os.environ[f"FEED_DSN_{n}"], True)
    yield e
    e.close()
    fdb.reload_config()


def _topology(etcd):
    t = topo_mod.Topology(topo_mod.EtcdClient([etcd.url]))
    t.wait_ready(10)
    return t


def _uid(p):
    return f"{p}_{uuid.uuid4().hex[:8]}"


def test_topology_from_etcd_routes_like_static(etcd):
    t = _topology(etcd)
    ring = json.loads(etcd.get("/feed/ring"))
    assert ring["ring"] == os.environ["FEED_NODES"].split(",") and ring["epoch"] == 1
    author = _uid("t")
    pin, _ = repo.create_pin(storage=FakeStorage(), author_id=author, title="x", description="",
                             tags=["a"], event_id=str(uuid.uuid4()), filename="x.png",
                             data=b"1", content_type="image/png")
    assert repo.get_pin(author, pin["pin_id"]) is not None
    assert t.describe()["mode"] == "etcd"


@pytest.mark.skipif(not os.environ.get("FEED_FAILOVER_PRIMARY"), reason="нужен кластер мастер+реплика")
def test_master_failure_like_survives_and_nothing_acknowledged_is_lost(etcd, monkeypatch):
    primary, standby = os.environ["FEED_FAILOVER_PRIMARY"], os.environ["FEED_FAILOVER_STANDBY"]
    # узел N3 — кластер мастер+реплика
    _register_member(etcd, "feed-n3", "n3a", primary, True)
    _register_member(etcd, "feed-n3", "n3b", standby, False)
    t = _topology(etcd)
    author = next(a for a in (_uid("fo") for _ in range(500)) if fdb.node_for(a) == "N3")
    pin, _ = repo.create_pin(storage=FakeStorage(), author_id=author, title="до отказа", description="",
                             tags=["горы"], event_id=str(uuid.uuid4()), filename="x.png",
                             data=b"1", content_type="image/png")
    acked = []
    for i in range(20):
        ev = str(uuid.uuid4())
        repo.record_interaction("like" if i == 0 else "view", _uid("u"), pin["pin_id"], ev, author)
        acked.append(ev)

    def patroni_failover():
        time.sleep(0.5)
        subprocess.run(os.environ["FEED_FAILOVER_STOP"], shell=True, check=True)
        time.sleep(3)  # Patroni: истечение lease мастера, выборы
        with get_conn_by_dsn(standby) as c:
            c.execute("SELECT pg_promote()")
        time.sleep(1)
        _register_member(etcd, "feed-n3", "n3b", standby, True)

    threading.Thread(target=patroni_failover, daemon=True).start()
    time.sleep(1)  # мастер уже лежит
    started = time.monotonic()
    liker, ev = _uid("liker"), str(uuid.uuid4())
    res = failover.with_retry(
        lambda _a: repo.record_interaction("like", liker, pin["pin_id"], ev, author), t.refresh, window=30)
    took = time.monotonic() - started
    assert res["likes_count"] == 2 and res["liked_by_me"]
    assert t.snapshot.leaders["N3"].startswith(f"postgresql://feed:feed@{urlparse(standby).hostname}:{urlparse(standby).port}")
    # повтор того же запроса после переключения — дубль, не второй лайк
    again = repo.record_interaction("like", liker, pin["pin_id"], ev, author)
    assert again["duplicate"] and again["likes_count"] == 2
    # всё подтверждённое до отказа есть на новом мастере (синхронная реплика)
    with get_conn_by_dsn(standby) as c:
        n = c.execute("SELECT count(*) AS n FROM interactions WHERE event_id = ANY(%s::uuid[])",
                      (acked,)).fetchone()["n"]
    assert n == len(acked)
    print(f"failover: лайк прошёл через {took:.1f} с после отказа мастера")


@pytest.mark.skipif(not os.environ.get("FEED_DSN_N4"), reason="нужна база для узла N4")
def test_add_and_remove_node_online_while_likes_flow(etcd, monkeypatch):
    _register_member(etcd, "feed-n4", "n4a", os.environ["FEED_DSN_N4"], True)
    with get_conn_by_dsn(os.environ["FEED_DSN_N4"]) as c:   # N4 пустой
        for t in ("outbox", "interactions", "pins", "author_pins", "pin_requests", "sessions", "users"):
            c.execute(f"DELETE FROM {t}")
    monkeypatch.setattr(topo_mod, "REFRESH_SECONDS", 0.3)
    t = _topology(etcd)
    t.start()
    old_nodes = list(t.snapshot.ring.ring)

    storage = FakeStorage()
    authors = [_uid("mig") for _ in range(30)]
    pins = []
    for a in authors:
        for i in range(2):
            p, _ = repo.create_pin(storage=storage, author_id=a, title=f"p{i}", description="",
                                   tags=["горы"], event_id=str(uuid.uuid4()), filename="x.png",
                                   data=b"1", content_type="image/png")
            pins.append((a, p["pin_id"]))

    stop = threading.Event()
    done_likes: list[str] = []
    errors: list[str] = []

    def liker():
        while not stop.is_set():
            a, pid = random.choice(pins)
            ev = str(uuid.uuid4())
            try:
                failover.with_retry(lambda _x: repo.record_interaction("view", _uid("v"), pid, ev, a),
                                    t.refresh, window=10)
                done_likes.append(ev)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(0.02)

    th = threading.Thread(target=liker, daemon=True)
    th.start()
    lines = []
    try:
        report = migrate.add_node(t, "N4", "feed-n4", grace=1.5, log=lines.append)
        time.sleep(0.5)
        new_ring = fdb.build_ring(old_nodes + ["N4"])
        # consistent hashing: ключи переехали только на N4
        assert report.keys_moved > 0
        assert all(dst == "N4" for (_src, dst) in report.routes)
        assert t.snapshot.ring.ring == old_nodes + ["N4"]
        _check_placement(authors, pins, new_ring.get_node)

        report2 = migrate.remove_node(t, "N4", grace=1.5, log=lines.append)
        assert all(src == "N4" for (src, _dst) in report2.routes)
        time.sleep(0.5)
        _check_placement(authors, pins, fdb.build_ring(old_nodes).get_node)
    finally:
        stop.set()
        th.join(5)
        t.stop()
    assert not errors, errors[:3]
    # каждое подтверждённое во время переноса событие лежит ровно в одном месте
    total = 0
    for n in old_nodes:
        with get_conn_by_dsn(os.environ[f"FEED_DSN_{n}"]) as c:
            total += c.execute("SELECT count(*) AS n FROM interactions WHERE event_id = ANY(%s::uuid[])",
                               (done_likes,)).fetchone()["n"]
    assert total == len(done_likes) and len(done_likes) > 20
    print("\n".join(lines))
    print(f"во время переноса записано событий: {len(done_likes)}, ошибок: {len(errors)}")


def _check_placement(authors, pins, owner_of):
    ring, dsns = fdb.current_topology()
    for a in authors:
        holders = []
        for n, dsn in dsns.items():
            with get_conn_by_dsn(dsn) as c:
                if c.execute("SELECT count(*) AS n FROM pins WHERE author_id = %s", (a,)).fetchone()["n"]:
                    holders.append(n)
        assert holders == [owner_of(a)], (a, holders, owner_of(a))
    for a, pid in pins:
        assert repo.get_pin(a, pid) is not None
