"""Топология Feed DB из etcd: какие узлы в хеш-кольце и где сейчас мастер каждого.

Каждый узел кольца (N1, N2, ...) — отдельный кластер Patroni (scope feed-n1, ...)
из мастера и синхронной реплики на разных физических машинах. Patroni хранит
в etcd ключ лидера кластера и описание каждого участника:

    /service/<scope>/leader            -> имя участника-мастера
    /service/<scope>/members/<имя>     -> JSON с conn_url "postgres://host:port/postgres"

Состав кольца хранит сам Feed (его пишет утилита server.migrate):

    /feed/ring -> {"epoch": 3, "ring": ["N1","N2","N3"], "scopes": {"N1": "feed-n1", ...}}

В scopes могут быть узлы, которых ещё нет в ring (узел подключается).

Feed перечитывает топологию раз в FEED_TOPOLOGY_REFRESH секунд и сразу после
ошибки записи. etcd опрашивается через его HTTP/JSON-шлюз (/v3/kv/range),
поэтому отдельный клиент etcd не нужен. Если etcd недоступен, остаётся последняя
известная топология: чтение и запись в здоровые узлы продолжаются.

Без FEED_ETCD_HOSTS топология берётся из переменных окружения (FEED_NODES и
FEED_DSN_<узел>), как раньше: так работают тесты и стенд без Patroni.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import feed.db.connection as fdb

log = logging.getLogger("feed.topology")

RING_KEY = os.getenv("FEED_RING_KEY", "/feed/ring")
PATRONI_NAMESPACE = os.getenv("PATRONI_NAMESPACE", "/service/").rstrip("/") + "/"
DB_USER = os.getenv("FEED_DB_USER", "feed")
DB_PASSWORD = os.getenv("FEED_DB_PASSWORD", "feed")
DB_NAME = os.getenv("FEED_DB_NAME", "feed")
REFRESH_SECONDS = float(os.getenv("FEED_TOPOLOGY_REFRESH", "2"))


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _unb64(s: str) -> str:
    return base64.b64decode(s).decode()


class EtcdError(Exception):
    pass


class EtcdClient:
    """Минимальный клиент etcd v3 через HTTP-шлюз: get, get_prefix, put, compare-and-put."""

    def __init__(self, endpoints: list[str], timeout: float = 1.5):
        if not endpoints:
            raise EtcdError("не заданы адреса etcd")
        self.endpoints = [e if e.startswith("http") else f"http://{e}" for e in endpoints]
        self.timeout = timeout

    def _post(self, path: str, body: dict) -> dict:
        import urllib.request

        data = json.dumps(body).encode()
        last: Exception | None = None
        for ep in self.endpoints:
            try:
                req = urllib.request.Request(f"{ep}{path}", data=data,
                                             headers={"Content-Type": "application/json"})
                # прокси из окружения к etcd в локальной сети не нужен
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=self.timeout) as r:
                    return json.loads(r.read() or b"{}")
            except Exception as exc:  # следующий участник кластера etcd
                last = exc
        raise EtcdError(f"etcd недоступен: {last}")

    def get(self, key: str) -> tuple[str | None, int]:
        """(значение, mod_revision); (None, 0), если ключа нет."""
        res = self._post("/v3/kv/range", {"key": _b64(key)})
        kvs = res.get("kvs") or []
        if not kvs:
            return None, 0
        return _unb64(kvs[0].get("value", "")), int(kvs[0].get("mod_revision", 0))

    def put(self, key: str, value: str) -> None:
        self._post("/v3/kv/put", {"key": _b64(key), "value": _b64(value)})

    def compare_and_put(self, key: str, value: str, mod_revision: int) -> bool:
        """Записать, только если ключ не менялся с mod_revision (0 — ключа не было)."""
        res = self._post("/v3/kv/txn", {
            "compare": [{"key": _b64(key), "target": "MOD", "result": "EQUAL",
                         "mod_revision": str(mod_revision)}],
            "success": [{"request_put": {"key": _b64(key), "value": _b64(value)}}],
        })
        return bool(res.get("succeeded"))


@dataclass
class RingConfig:
    epoch: int
    ring: list[str]
    scopes: dict[str, str]

    @classmethod
    def from_json(cls, text: str) -> "RingConfig":
        d = json.loads(text)
        return cls(int(d["epoch"]), list(d["ring"]), dict(d["scopes"]))

    def to_json(self) -> str:
        return json.dumps({"epoch": self.epoch, "ring": self.ring, "scopes": self.scopes},
                          ensure_ascii=False)


def default_ring_config() -> RingConfig:
    nodes = fdb.parse_node_ids()
    return RingConfig(1, nodes, {n: os.getenv(f"FEED_SCOPE_{n}", f"feed-{n.lower()}") for n in nodes})


def member_dsn(conn_url: str) -> str:
    """conn_url участника Patroni -> DSN базы Feed на этом участнике."""
    u = urlparse(conn_url)
    host, port = u.hostname, u.port or 5432
    db = u.path.lstrip("/")
    if not db or db == "postgres":     # Patroni всегда публикует базу postgres
        db = DB_NAME
    return (f"postgresql://{DB_USER}:{DB_PASSWORD}@{host}:{port}/{db}"
            f"?connect_timeout={int(os.getenv('FEED_DB_CONNECT_TIMEOUT', '3'))}")


@dataclass
class Snapshot:
    ring: RingConfig
    leaders: dict[str, str] = field(default_factory=dict)   # узел -> DSN мастера


class Topology:
    def __init__(self, etcd: EtcdClient | None):
        self.etcd = etcd
        self.snapshot: Snapshot | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @classmethod
    def from_env(cls) -> "Topology":
        hosts = [h.strip() for h in os.getenv("FEED_ETCD_HOSTS", "").split(",") if h.strip()]
        return cls(EtcdClient(hosts) if hosts else None)

    @property
    def dynamic(self) -> bool:
        return self.etcd is not None

    # -------------------------------------------------------------- чтение
    def read_ring(self) -> RingConfig:
        value, _ = self.etcd.get(RING_KEY)
        if value is None:
            cfg = default_ring_config()
            # первым записывает тот, кто стартовал раньше; остальные прочитают его версию
            if not self.etcd.compare_and_put(RING_KEY, cfg.to_json(), 0):
                value, _ = self.etcd.get(RING_KEY)
                cfg = RingConfig.from_json(value)
            return cfg
        return RingConfig.from_json(value)

    def leader_dsn(self, scope: str) -> str | None:
        name, _ = self.etcd.get(f"{PATRONI_NAMESPACE}{scope}/leader")
        if not name:
            return None
        member, _ = self.etcd.get(f"{PATRONI_NAMESPACE}{scope}/members/{name}")
        if not member:
            return None
        conn_url = json.loads(member).get("conn_url")
        return member_dsn(conn_url) if conn_url else None

    def refresh(self) -> bool:
        """Перечитать etcd и применить. True, если что-то поменялось."""
        if not self.dynamic:
            return False
        with self._lock:
            try:
                ring = self.read_ring()
                leaders = {}
                old = self.snapshot.leaders if self.snapshot else {}
                for node, scope in ring.scopes.items():
                    dsn = self.leader_dsn(scope)
                    if dsn:
                        leaders[node] = dsn
                    elif node in old:
                        # лидера нет (идут выборы): оставляем прежний адрес, запись повторится
                        leaders[node] = old[node]
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("топология: etcd недоступен, оставляю прежнюю (%s)", exc)
                return False
            missing = [n for n in ring.ring if n not in leaders]
            if missing:
                self.last_error = f"нет мастера для {', '.join(missing)}"
                if self.snapshot is None:
                    log.warning("топология: %s, жду", self.last_error)
                    return False
                for n in missing:
                    if n in self.snapshot.leaders:
                        leaders[n] = self.snapshot.leaders[n]
                missing = [n for n in ring.ring if n not in leaders]
                if missing:
                    return False
            changed = (self.snapshot is None or self.snapshot.ring.epoch != ring.epoch
                       or self.snapshot.ring.ring != ring.ring or self.snapshot.leaders != leaders)
            if changed:
                fdb.set_topology(ring.ring, leaders)
                if self.snapshot is not None:
                    for node, dsn in leaders.items():
                        if self.snapshot.leaders.get(node) != dsn:
                            log.warning("топология: новый мастер узла %s", node)
                    if self.snapshot.ring.epoch != ring.epoch:
                        log.warning("топология: кольцо epoch %d -> %d: %s",
                                    self.snapshot.ring.epoch, ring.epoch, ring.ring)
                self.snapshot = Snapshot(ring, leaders)
            return changed

    def wait_ready(self, timeout: float = 120.0) -> None:
        if not self.dynamic:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.refresh()
            if self.snapshot is not None:
                return
            time.sleep(1)
        raise RuntimeError(f"топология Feed DB не получена: {self.last_error}")

    # -------------------------------------------------------------- фон
    def start(self) -> None:
        if not self.dynamic:
            return
        self._thread = threading.Thread(target=self._run, name="topology", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            self.refresh()

    def describe(self) -> dict:
        if not self.dynamic:
            ring, dsns = fdb.current_topology()
            return {"mode": "static", "ring": ring}
        s = self.snapshot
        return {
            "mode": "etcd",
            "epoch": s.ring.epoch if s else None,
            "ring": s.ring.ring if s else [],
            "scopes": s.ring.scopes if s else {},
            "leaders": {n: urlparse(d).hostname + ":" + str(urlparse(d).port)
                        for n, d in (s.leaders.items() if s else [])},
            "last_error": self.last_error,
        }
