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
class Table:
    """Таблица группы: как выбрать строки ключа и что делать при повторной вставке."""

    name: str
    where: str            # условие с одним %s — ключ
    key_column: str | None = None   # колонка, из которой берутся ключи узла
    on_conflict: str = "ON CONFLICT DO NOTHING"


@dataclass(frozen=True)
class Group:
    name: str
    key_cast: str
    tables: tuple[Table, ...]


_OF_AUTHOR = "pin_id IN (SELECT pin_id FROM pins WHERE author_id = %s)"

# Ключ шардирования — логин пользователя (он же автор пина). Всё, что относится к
# пользователю и его пинам, лежит на одном узле и переносится вместе: родители раньше
# детей при вставке, дети раньше родителей при удалении.
GROUPS = (
    Group("author", "text", (
        Table("users", "user_id = %s", "user_id",
              "ON CONFLICT (user_id) DO UPDATE SET password_hash = EXCLUDED.password_hash, "
              "display_name = EXCLUDED.display_name, avatar_url = EXCLUDED.avatar_url"),
        Table("sessions", "user_id = %s", "user_id"),
        Table("pin_requests", "user_id = %s", "user_id",
              "ON CONFLICT (user_id, event_id) DO UPDATE SET status = EXCLUDED.status, "
              "updated_at = EXCLUDED.updated_at"),
        Table("author_pins", "author_id = %s", "author_id"),
        Table("pins", "author_id = %s", "author_id"),
        Table("interactions", _OF_AUTHOR),
        Table("outbox", _OF_AUTHOR, None,
              "ON CONFLICT (event_id) DO UPDATE SET sent_at = coalesce(outbox.sent_at, EXCLUDED.sent_at)"),
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
    for t in group.tables:
        if t.key_column:
            for row in conn.execute(f"SELECT DISTINCT {t.key_column}::text AS k FROM {t.name}").fetchall():
                keys.add(row["k"])
    return keys


def _count(conn, group: Group, t: Table, key: str) -> int:
    return int(conn.execute(
        f"SELECT count(*) AS n FROM {t.name} WHERE {t.where}", (key,)
    ).fetchone()["n"])


def _snapshot(src_dsn: str, group: Group, key: str) -> list:
    snapshot = []
    with get_conn_by_dsn(src_dsn) as src:
        for t in group.tables:
            row = src.execute(
                f"SELECT coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) AS rows, count(*) AS n "
                f"FROM {t.name} x WHERE {t.where}", (key,)
            ).fetchone()
            rows = json.loads(row["rows"]) if isinstance(row["rows"], str) else row["rows"]
            snapshot.append((t, rows, int(row["n"])))
    return snapshot


def _copy(dst_dsn: str, group: Group, key: str, snapshot: list) -> None:
    with get_conn_by_dsn(dst_dsn) as dst:
        for t, rows, n in snapshot:
            if n:
                dst.execute(
                    f"INSERT INTO {t.name} SELECT * FROM "
                    f"jsonb_populate_recordset(NULL::{t.name}, %s::jsonb) {t.on_conflict}",
                    (json.dumps(rows),),
                )
            if _count(dst, group, t, key) < n:
                raise RebalanceError(f"{t.name}: копия ключа {key} неполная, перенос отменён")


def copy_key(src_dsn: str, dst_dsn: str, group: Group, key: str) -> int:
    """Скопировать строки ключа, не удаляя их на источнике (первый проход онлайн-переноса)."""
    snapshot = _snapshot(src_dsn, group, key)
    _copy(dst_dsn, group, key, snapshot)
    return sum(n for _, _, n in snapshot)


def _move_key(src_dsn: str, dst_dsn: str, group: Group, key: str) -> int:
    snapshot = _snapshot(src_dsn, group, key)
    _copy(dst_dsn, group, key, snapshot)
    with get_conn_by_dsn(src_dsn) as src:
        for t, _, n in snapshot:
            if _count(src, group, t, key) != n:
                raise RebalanceError(
                    f"{t.name}: строки ключа {key} изменились во время переноса, "
                    "запустите перенос ещё раз"
                )
        for t, _, n in reversed(snapshot):
            if n:
                src.execute(f"DELETE FROM {t.name} WHERE {t.where}", (key,))
    return sum(n for _, _, n in snapshot)


def rebalance(
    old_nodes: list[str],
    new_nodes: list[str],
    *,
    dsns: dict[str, str] | None = None,
    dry_run: bool = False,
    only_keys: Callable[[str], bool] | None = None,
    log: Callable[[str], None] = lambda message: None,
    copy_only: bool = False,
) -> Report:
    """Перенести ключи, владелец которых в кольце new_nodes не совпадает с текущим узлом.

    copy_only=True только копирует (источник не трогается) — первый проход
    онлайн-переноса, пока Feed ещё пишет по старому кольцу (server/migrate.py).
    """
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
            mover = copy_key if copy_only else _move_key
            report.rows_moved += mover(dsns[src_node], dsns[owner], group, key)
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
