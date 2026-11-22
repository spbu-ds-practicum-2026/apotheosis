from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from typing import Callable, ContextManager, Iterator

from feed.db.ring import DEFAULT_TOKENS_PER_NODE, HashRing

DEFAULT_DSN = "postgresql://feed:feed@localhost:5432/feed"
DEFAULT_NODES = "N1"
_NODE_ID_RE = re.compile(r"[A-Za-z0-9_]{1,16}")


class ConfigError(Exception):
    pass


_lock = threading.Lock()
_config: tuple[HashRing, dict[str, str]] | None = None
_pools: dict[str, object] = {}

ConnectionFactory = Callable[[str], ContextManager]
_factory: ConnectionFactory | None = None


def parse_node_ids(env: dict[str, str] | None = None) -> list[str]:
    env = os.environ if env is None else env
    nodes = [n.strip() for n in env.get("FEED_NODES", DEFAULT_NODES).split(",") if n.strip()]
    if not nodes:
        raise ConfigError("FEED_NODES пуст")
    for node in nodes:
        if not _NODE_ID_RE.fullmatch(node):
            raise ConfigError(f"некорректный идентификатор узла: {node!r}")
    if len(set(nodes)) != len(nodes):
        raise ConfigError("FEED_NODES: идентификаторы узлов повторяются")
    return nodes


def load_node_dsns(node_ids: list[str], env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    dsns: dict[str, str] = {}
    for node in node_ids:
        dsn = env.get(f"FEED_DSN_{node}")
        if dsn is None and len(node_ids) == 1:
            dsn = env.get("FEED_DB_DSN", DEFAULT_DSN)
        if dsn is None:
            raise ConfigError(f"не задан FEED_DSN_{node}")
        dsns[node] = dsn
    if len(set(dsns.values())) != len(dsns):
        raise ConfigError("два узла указывают на одну и ту же базу (одинаковый DSN)")
    return dsns


def build_ring(node_ids) -> HashRing:
    return HashRing(node_ids, DEFAULT_TOKENS_PER_NODE)


def reload_config() -> None:
    global _config
    with _lock:
        _config = None
    close_all()


def _cfg() -> tuple[HashRing, dict[str, str]]:
    global _config
    if _config is None:
        with _lock:
            if _config is None:
                nodes = parse_node_ids()
                _config = (build_ring(nodes), load_node_dsns(nodes))
    return _config


def node_ids() -> list[str]:
    return list(_cfg()[0].node_ids)


def node_for(key: str) -> str:
    return _cfg()[0].get_node(key)


def dsn_for_node(node_id: str) -> str:
    return _cfg()[1][node_id]


def dsn_for_key(key: str) -> str:
    return dsn_for_node(node_for(key))


def all_node_dsns() -> list[str]:
    ring, dsns = _cfg()
    return [dsns[n] for n in ring.node_ids]


def set_connection_factory(factory: ConnectionFactory | None) -> None:
    global _factory
    _factory = factory


def _psycopg_connection(dsn: str) -> ContextManager:
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = _pools.get(dsn)
    if pool is None:
        with _lock:
            pool = _pools.get(dsn)
            if pool is None:
                pool = ConnectionPool(
                    dsn, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=True
                )
                _pools[dsn] = pool
    return pool.connection()


@contextmanager
def get_conn_by_dsn(dsn: str) -> Iterator:
    factory = _factory or _psycopg_connection
    with factory(dsn) as conn:
        yield conn


@contextmanager
def get_conn(key: str) -> Iterator:
    with get_conn_by_dsn(dsn_for_key(key)) as conn:
        yield conn


def close_all() -> None:
    with _lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()
