from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Callable

import feed.db.connection as fdb
from feed.db.connection import ConfigError, get_conn_by_dsn


class RebalanceError(Exception):
    pass


@dataclass(frozen=True)
class Group:

    name: str
    key_cast: str
    tables: tuple[tuple[str, str], ...]


GROUPS = (
    Group("user", "text", (
        ("users", "user_id"),
        ("sessions", "user_id"),
        ("pin_requests", "user_id"),
        ("author_pins", "author_id"),
    )),
    Group("pin", "uuid", (
        ("pins", "pin_id"),
        ("interactions", "pin_id"),
        ("outbox", "pin_id"),
    )),
)


@dataclass
class Report:
    dry_run: bool
    keys_moved: int = 0
    rows_moved: int = 0
    routes: dict[tuple[str, str], int] = field(default_factory=dict)


def _keys_on_node(conn, group: Group) -> set[str]:
    keys: set[str] = set()
    for table, col in group.tables:
        for row in conn.execute(f"SELECT DISTINCT {col}::text AS k FROM {table}").fetchall():
            keys.add(row["k"])
    return keys


def _count(conn, group: Group, table: str, col: str, key: str) -> int:
    return int(conn.execute(
        f"SELECT count(*) AS n FROM {table} WHERE {col} = %s::{group.key_cast}", (key,)
    ).fetchone()["n"])


def _move_key(src_dsn: str, dst_dsn: str, group: Group, key: str) -> int:
    snapshot = []
    with get_conn_by_dsn(src_dsn) as src:
        for table, col in group.tables:
            row = src.execute(
                f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) AS rows, count(*) AS n "
                f"FROM {table} t WHERE {col} = %s::{group.key_cast}", (key,)
            ).fetchone()
            rows = json.loads(row["rows"]) if isinstance(row["rows"], str) else row["rows"]
            snapshot.append((table, col, rows, int(row["n"])))

    with get_conn_by_dsn(dst_dsn) as dst:
        for table, col, rows, n in snapshot:
            if n:
                dst.execute(
                    f"INSERT INTO {table} SELECT * FROM "
                    f"jsonb_populate_recordset(NULL::{table}, %s::jsonb) ON CONFLICT DO NOTHING",
                    (json.dumps(rows),),
                )
            if _count(dst, group, table, col, key) < n:
                raise RebalanceError(f"{table}: копия ключа {key} неполная, перенос отменён")

    with get_conn_by_dsn(src_dsn) as src:
        for table, col, _, n in snapshot:
            if _count(src, group, table, col, key) != n:
                raise RebalanceError(
                    f"{table}: строки ключа {key} изменились во время переноса. "
                    "Остановите запись в Feed и запустите перенос снова."
                )
        for table, col, _, n in reversed(snapshot):
            if n:
                src.execute(f"DELETE FROM {table} WHERE {col} = %s::{group.key_cast}", (key,))
    return sum(n for _, _, _, n in snapshot)


def rebalance(
    old_nodes: list[str],
    new_nodes: list[str],
    *,
    dsns: dict[str, str] | None = None,
    dry_run: bool = False,
    only_keys: Callable[[str], bool] | None = None,
    log: Callable[[str], None] = lambda message: None,
) -> Report:
    union = sorted(set(old_nodes) | set(new_nodes))
    dsns = dsns or fdb.load_node_dsns(union)
    missing = [n for n in union if n not in dsns]
    if missing:
        raise ConfigError(f"не заданы DSN узлов: {', '.join(missing)}")
    if len({dsns[n] for n in union}) != len(union):
        raise ConfigError("два узла указывают на одну базу: перенос удалил бы данные")

    new_ring = fdb.build_ring(new_nodes)
    report = Report(dry_run=dry_run)

    for src_node in sorted(old_nodes):
        moves: list[tuple[Group, str, str]] = []
        with get_conn_by_dsn(dsns[src_node]) as conn:
            for group in GROUPS:
                for key in sorted(_keys_on_node(conn, group)):
                    if only_keys is not None and not only_keys(key):
                        continue
                    owner = new_ring.get_node(key)
                    if owner != src_node:
                        moves.append((group, key, owner))
        log(f"{src_node}: ключей к переносу: {len(moves)}")
        for group, key, owner in moves:
            report.routes[(src_node, owner)] = report.routes.get((src_node, owner), 0) + 1
            if dry_run:
                continue
            report.rows_moved += _move_key(dsns[src_node], dsns[owner], group, key)
            report.keys_moved += 1
    if dry_run:
        report.keys_moved = sum(report.routes.values())
    return report


def _node_list(text: str) -> list[str]:
    nodes = [n.strip() for n in text.split(",") if n.strip()]
    if not nodes:
        raise ConfigError("список узлов пуст")
    return nodes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ребалансировка Feed DB при смене состава узлов")
    parser.add_argument("--old", required=True, help="текущие узлы, например N1,N2,N3")
    parser.add_argument("--new", required=True, help="новый состав, например N1,N2,N3,N4")
    parser.add_argument("--dry-run", action="store_true", help="только показать план")
    args = parser.parse_args(argv)
    try:
        report = rebalance(_node_list(args.old), _node_list(args.new),
                           dry_run=args.dry_run, log=print)
    except (ConfigError, RebalanceError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    for (src, dst), n in sorted(report.routes.items()):
        print(f"  {src} -> {dst}: {n} ключей")
    if report.dry_run:
        print(f"План: {report.keys_moved} ключей к переносу (ничего не изменено)")
    else:
        print(f"Готово: перенесено {report.keys_moved} ключей, {report.rows_moved} строк")
    return 0


if __name__ == "__main__":
    sys.exit(main())
