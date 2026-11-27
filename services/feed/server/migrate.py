"""Добавление и удаление узла Feed DB без остановки сервиса (consistent hashing).

    python -m server.migrate status
    python -m server.migrate add N4 [--scope feed-n4]
    python -m server.migrate remove N4

Узел — отдельный кластер Patroni (мастер + реплика), уже запущенный и видимый в
etcd под своим scope. Ключ шардирования — логин автора; при смене состава кольца
переезжают только ключи, у которых сменился владелец: при добавлении N4 — только
ключи, попавшие на дуги N4 (примерно 1/N от всех), остальные остаются на месте.

Порядок переноса (Feed не останавливается, лайки идут всё время):
1. Узел регистрируется в /feed/ring в scopes, но не в ring: Feed узнаёт его адрес.
2. Копирование: строки переезжающих ключей копируются на новых владельцев,
   на старых узлах они остаются. Feed пока пишет и читает по старому кольцу.
3. Переключение: в /feed/ring записывается новое кольцо (epoch + 1). Все
   экземпляры Feed перечитывают его не позже чем через FEED_TOPOLOGY_REFRESH.
4. Пауза grace (2 × период обновления + запас), чтобы все Feed точно переключились.
5. Перенос: строки копируются ещё раз (догоняются записи, сделанные до
   переключения), сверяется число строк, и только потом они удаляются со старого узла.
   Если за это время строки ключа поменялись — проход повторяется.
6. При удалении узла он убирается из scopes.

Все вставки идемпотентны (ON CONFLICT), поэтому прерванный перенос можно запустить снова.
"""
from __future__ import annotations

import argparse
import sys
import time

import feed.db.connection as fdb
from feed.db import rebalance as rb
from feed.db.connection import get_conn_by_dsn

from server import repository
from server.topology import REFRESH_SECONDS, RING_KEY, RingConfig, Topology


class MigrateError(Exception):
    pass


def _publish(topo: Topology, change) -> RingConfig:
    """Изменить /feed/ring атомарно (compare-and-put по mod_revision)."""
    for _ in range(10):
        value, rev = topo.etcd.get(RING_KEY)
        cfg = RingConfig.from_json(value) if value else topo.read_ring()
        if value is None:
            value, rev = topo.etcd.get(RING_KEY)
            cfg = RingConfig.from_json(value)
        new = change(RingConfig(cfg.epoch + 1, list(cfg.ring), dict(cfg.scopes)))
        if topo.etcd.compare_and_put(RING_KEY, new.to_json(), rev):
            return new
        time.sleep(0.2)
    raise MigrateError("не удалось обновить /feed/ring: его постоянно меняют")


def _wait_leader(topo: Topology, node: str, timeout: float = 120) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        topo.refresh()
        if topo.snapshot and node in topo.snapshot.leaders:
            return dict(topo.snapshot.leaders)
        time.sleep(1)
    raise MigrateError(f"у узла {node} нет мастера в etcd: запущен ли его кластер Patroni?")


def _apply_extra_schema(dsn: str) -> None:
    sql = repository.EXTRA_SCHEMA.read_text(encoding="utf-8")
    with get_conn_by_dsn(dsn) as conn:
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                conn.execute(stmt)


def _move_with_retries(old, new, dsns, log, attempts: int = 5) -> rb.Report:
    for attempt in range(1, attempts + 1):
        try:
            return rb.rebalance(old, new, dsns=dsns, log=log)
        except rb.RebalanceError as exc:
            log(f"  проход {attempt}: {exc}; повторяю")
            time.sleep(1)
    raise MigrateError("строки продолжают меняться на старом узле: Feed не переключился?")


def _print_report(title: str, report: rb.Report, log) -> None:
    log(title)
    for (src, dst), n in sorted(report.routes.items()):
        log(f"  {src} -> {dst}: {n} ключей")
    log(f"  всего ключей: {report.keys_moved}, строк: {report.rows_moved}")


def change_ring(topo: Topology, old: list[str], new: list[str], dsns: dict[str, str],
                grace: float, log=print) -> rb.Report:
    log(f"1/3 копирование ключей для кольца {new} (Feed работает по старому {old})")
    copied = rb.rebalance(old, new, dsns=dsns, log=log, copy_only=True)
    _print_report("   скопировано:", copied, log)

    cfg = _publish(topo, lambda c: RingConfig(c.epoch, list(new), c.scopes))
    log(f"2/3 новое кольцо опубликовано: epoch {cfg.epoch}, ждём {grace:.0f} с, пока Feed переключится")
    time.sleep(grace)

    log("3/3 догоняем изменения и удаляем ключи со старых узлов")
    moved = _move_with_retries(old, new, dsns, log)
    _print_report("   перенесено:", moved, log)
    leftover = rb.rebalance(new, new, dsns=dsns, dry_run=True)
    if leftover.keys_moved:
        raise MigrateError(f"после переноса остались чужие ключи: {leftover.keys_moved}")
    return moved


def add_node(topo: Topology, node: str, scope: str, grace: float, log=print) -> rb.Report:
    cfg = topo.read_ring()
    if node in cfg.ring:
        raise MigrateError(f"{node} уже в кольце {cfg.ring}")
    _publish(topo, lambda c: RingConfig(c.epoch, c.ring, {**c.scopes, node: scope}))
    log(f"узел {node} (scope {scope}) зарегистрирован, ищу его мастер")
    dsns = _wait_leader(topo, node)
    _apply_extra_schema(dsns[node])
    old = list(cfg.ring)
    new = old + [node]
    return change_ring(topo, old, new, dsns, grace, log)


def remove_node(topo: Topology, node: str, grace: float, log=print) -> rb.Report:
    cfg = topo.read_ring()
    if node not in cfg.ring:
        raise MigrateError(f"{node} нет в кольце {cfg.ring}")
    if len(cfg.ring) == 1:
        raise MigrateError("нельзя удалить последний узел")
    dsns = _wait_leader(topo, node)
    old = list(cfg.ring)
    new = [n for n in old if n != node]
    report = change_ring(topo, old, new, dsns, grace, log)
    _publish(topo, lambda c: RingConfig(c.epoch, c.ring, {k: v for k, v in c.scopes.items() if k != node}))
    log(f"узел {node} выведен из кольца; его кластер Patroni можно останавливать")
    return report


def status(topo: Topology, log=print) -> None:
    topo.refresh()
    d = topo.describe()
    log(f"кольцо (epoch {d.get('epoch')}): {', '.join(d.get('ring', []))}")
    for node, scope in d.get("scopes", {}).items():
        mark = "" if node in d.get("ring", []) else "  (не в кольце)"
        log(f"  {node}: scope {scope}, мастер {d.get('leaders', {}).get(node, 'нет')}{mark}")
    ring, dsns = fdb.current_topology()
    counts = {}
    for n in ring:
        try:
            with get_conn_by_dsn(dsns[n]) as conn:
                counts[n] = (conn.execute("SELECT count(*) AS n FROM pins").fetchone()["n"],
                             conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"])
        except Exception as exc:
            counts[n] = (f"недоступен: {type(exc).__name__}", "")
    for n, (pins, users) in counts.items():
        log(f"  {n}: пинов {pins}, пользователей {users}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Состав кольца Feed DB: добавить или убрать узел")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("add")
    a.add_argument("node")
    a.add_argument("--scope", help="scope кластера Patroni (по умолчанию feed-<узел>)")
    a.add_argument("--grace", type=float, default=2 * REFRESH_SECONDS + 3)
    r = sub.add_parser("remove")
    r.add_argument("node")
    r.add_argument("--grace", type=float, default=2 * REFRESH_SECONDS + 3)
    args = p.parse_args(argv)

    topo = Topology.from_env()
    if not topo.dynamic:
        print("Ошибка: задайте FEED_ETCD_HOSTS (кольцо хранится в etcd)", file=sys.stderr)
        return 1
    try:
        topo.wait_ready(60)
        if args.cmd == "status":
            status(topo)
        elif args.cmd == "add":
            add_node(topo, args.node, args.scope or f"feed-{args.node.lower()}", args.grace)
            status(topo)
        else:
            remove_node(topo, args.node, args.grace)
            status(topo)
    except (MigrateError, rb.RebalanceError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
