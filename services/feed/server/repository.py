"""Операции Feed с Feed DB: пины, лайки, просмотры, outbox.

Шардирование. Пин, его лайки и просмотры, события outbox, запись
идемпотентности создания пина и индекс публикаций автора лежат на узле
по ключу author_id (userId автора). Поэтому создание пина и лайк с событием
в outbox — каждый одна локальная транзакция одного узла, без распределённых
транзакций. Клиент передаёт author_id вместе с pinId (он есть в каждой
карточке ленты); если не передал — узел пина ищется обходом всех узлов.

Одна транзакция = один блок `with get_conn(...)`: соединение пула
фиксирует транзакцию при выходе из блока и откатывает при исключении.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

from feed.auth import AuthError, ensure_profile
from feed.db.connection import (
    all_node_dsns,
    get_conn,
    get_conn_by_dsn,
    node_for,
    node_ids,
    dsn_for_node,
)

from server import config
from server.events import KIND_TO_EVENT, PIN_CREATED, build_event

log = logging.getLogger("feed.repository")

EXTRA_SCHEMA = Path(__file__).with_name("schema_extra.sql")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

PIN_COLUMNS = "pin_id, author_id, title, description, tags, image_key, created_at"


class FeedError(Exception):
    status = 500


class BadRequest(FeedError):
    status = 400


class NotFound(FeedError):
    status = 404


class Conflict(FeedError):
    status = 409


def parse_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise BadRequest(f"{field} должен быть UUID") from None


def shard_of(author_id: str) -> str:
    return node_for(author_id)


def apply_extra_schema() -> None:
    """Дополнения к схеме товарища, нужные Feed (идемпотентно, на каждом узле)."""
    sql = EXTRA_SCHEMA.read_text(encoding="utf-8")
    for node in node_ids():
        with get_conn_by_dsn(dsn_for_node(node)) as conn:
            for stmt in (s.strip() for s in sql.split(";")):
                if stmt:
                    conn.execute(stmt)


# ---------------------------------------------------------------- профили

_known_profiles: set[str] = set()


def ensure_user(user_id: str) -> None:
    """Профиль в users нужен для author_pins (FK). Вход идёт через Keycloak,
    поэтому профиль создаётся при первом обращении пользователя к Feed."""
    if user_id in _known_profiles:
        return
    try:
        ensure_profile(user_id)
    except AuthError as exc:
        raise BadRequest(f"недопустимый userId: {exc}") from None
    _known_profiles.add(user_id)


# ---------------------------------------------------------------- чтение пинов

def _pin_row_to_dict(row: dict) -> dict:
    return {
        "pin_id": str(row["pin_id"]),
        "author_id": row["author_id"],
        "title": row["title"],
        "description": row["description"] or "",
        "tags": list(row["tags"] or []),
        "image_key": row["image_key"],
        "created_at": row["created_at"],
    }


def _group_by_node(items: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for author_id, pin_id in items:
        groups.setdefault(shard_of(author_id), []).append(pin_id)
    return groups


def get_pins(items: list[tuple[str, str]]) -> dict[str, dict]:
    """Пины по парам (author_id, pin_id). Недоступный узел пропускается:
    его пины выпадают из порции, остальная лента отдаётся (деградация)."""
    found: dict[str, dict] = {}
    for node, pin_ids in _group_by_node(items).items():
        try:
            with get_conn_by_dsn(dsn_for_node(node)) as conn:
                rows = conn.execute(
                    f"SELECT {PIN_COLUMNS} FROM pins WHERE pin_id = ANY(%s::uuid[])",
                    (pin_ids,),
                ).fetchall()
        except Exception:
            log.warning("узел %s недоступен при чтении пинов", node, exc_info=True)
            continue
        for r in rows:
            found[str(r["pin_id"])] = _pin_row_to_dict(r)
    return found


def recent_pins(limit: int) -> list[dict]:
    """Недавние пины со всех узлов (запасной путь ленты, scatter-gather)."""
    pins: list[dict] = []
    for dsn in all_node_dsns():
        try:
            with get_conn_by_dsn(dsn) as conn:
                rows = conn.execute(
                    f"SELECT {PIN_COLUMNS} FROM pins ORDER BY created_at DESC, pin_id DESC LIMIT %s",
                    (limit,),
                ).fetchall()
        except Exception:
            log.warning("узел недоступен при чтении недавних пинов", exc_info=True)
            continue
        pins.extend(_pin_row_to_dict(r) for r in rows)
    pins.sort(key=lambda p: (p["created_at"], p["pin_id"]), reverse=True)
    return pins[:limit]


def like_stats(user_id: str, items: list[tuple[str, str]]) -> dict[str, tuple[int, bool]]:
    """pin_id -> (число лайков, лайкнул ли пользователь)."""
    stats: dict[str, tuple[int, bool]] = {}
    for node, pin_ids in _group_by_node(items).items():
        try:
            with get_conn_by_dsn(dsn_for_node(node)) as conn:
                rows = conn.execute(
                    "SELECT pin_id, count(*) AS n, bool_or(user_id = %s) AS mine "
                    "FROM interactions WHERE kind = 'like' AND pin_id = ANY(%s::uuid[]) "
                    "GROUP BY pin_id",
                    (user_id, pin_ids),
                ).fetchall()
        except Exception:
            log.warning("узел %s недоступен при подсчёте лайков", node, exc_info=True)
            continue
        for r in rows:
            stats[str(r["pin_id"])] = (int(r["n"]), bool(r["mine"]))
    return stats


def find_pin_author(pin_id: str) -> str | None:
    """Автор пина обходом всех узлов — только если клиент не прислал author_id."""
    for dsn in all_node_dsns():
        try:
            with get_conn_by_dsn(dsn) as conn:
                row = conn.execute(
                    "SELECT author_id FROM pins WHERE pin_id = %s::uuid", (pin_id,)
                ).fetchone()
        except Exception:
            log.warning("узел недоступен при поиске пина", exc_info=True)
            continue
        if row:
            return row["author_id"]
    return None


def get_pin(author_id: str, pin_id: str) -> dict | None:
    with get_conn(author_id) as conn:
        row = conn.execute(
            f"SELECT {PIN_COLUMNS} FROM pins WHERE pin_id = %s::uuid AND author_id = %s",
            (pin_id, author_id),
        ).fetchone()
    return _pin_row_to_dict(row) if row else None


# ---------------------------------------------------------------- лайк и просмотр

def record_interaction(
    kind: str, user_id: str, pin_id: str, event_id: str, author_id: str | None
) -> dict:
    """Лайк или просмотр с защитой от дублей по event_id.

    В одной транзакции узла пина: запись в interactions (event_id — PRIMARY
    KEY) и событие в outbox. Повтор с тем же event_id не вставляет ничего и
    возвращает результат первой операции. Второй лайк того же пина тем же
    пользователем с новым event_id тоже ничего не меняет (уникальный индекс
    из schema_extra.sql), счётчик не растёт.
    """
    if kind not in KIND_TO_EVENT:
        raise BadRequest(f"неизвестный тип взаимодействия: {kind}")
    pin_id = parse_uuid(pin_id, "pinId")
    event_id = parse_uuid(event_id, "event_id")

    author = author_id or find_pin_author(pin_id)
    if not author:
        raise NotFound("Пин не найден")
    shard = shard_of(author)

    with get_conn(author) as conn:
        pin = conn.execute(
            "SELECT author_id, tags FROM pins WHERE pin_id = %s::uuid AND author_id = %s",
            (pin_id, author),
        ).fetchone()
        if pin is None:
            pin_found_elsewhere = None
        else:
            inserted = conn.execute(
                "INSERT INTO interactions (event_id, pin_id, user_id, kind) "
                "VALUES (%s::uuid, %s::uuid, %s, %s) ON CONFLICT DO NOTHING RETURNING event_id",
                (event_id, pin_id, user_id, kind),
            ).fetchone()
            duplicate = inserted is None
            if duplicate:
                first = conn.execute(
                    "SELECT user_id, pin_id, kind FROM interactions WHERE event_id = %s::uuid",
                    (event_id,),
                ).fetchone()
                if first is not None and (
                    first["user_id"] != user_id or str(first["pin_id"]) != pin_id
                    or first["kind"] != kind
                ):
                    raise Conflict("event_id уже использован для другого действия")
            else:
                event = build_event(
                    event_id=event_id, event_type=KIND_TO_EVENT[kind], user_id=user_id,
                    pin_id=pin_id, author_id=author, shard=shard, tags=pin["tags"] or [],
                )
                conn.execute(
                    "INSERT INTO outbox (event_id, pin_id, kind, payload) "
                    "VALUES (%s::uuid, %s::uuid, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                    (event_id, pin_id, event["event_type"], _json(event)),
                )
            likes = conn.execute(
                "SELECT count(*) AS n, coalesce(bool_or(user_id = %s), false) AS mine "
                "FROM interactions WHERE pin_id = %s::uuid AND kind = 'like'",
                (user_id, pin_id),
            ).fetchone()
            pin_found_elsewhere = False

    if pin_found_elsewhere is None:
        # клиент прислал неверный author_id: один раз ищем пин по всем узлам
        real_author = find_pin_author(pin_id) if author_id else None
        if real_author and real_author != author_id:
            return record_interaction(kind, user_id, pin_id, event_id, real_author)
        raise NotFound("Пин не найден")

    result = {
        "status": "ok",
        "pinId": pin_id,
        "userId": user_id,
        "event_id": event_id,
        "duplicate": duplicate,
        "likes_count": int(likes["n"]),
        "liked_by_me": bool(likes["mine"]),
        "shard": shard,
    }
    return result


# ---------------------------------------------------------------- создание пина

def _safe_filename(name: str | None) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _SAFE_NAME_RE.sub("_", base).strip("._") or "pin"
    return base[:100]


def create_pin(
    *,
    storage,
    author_id: str,
    title: str,
    description: str,
    tags: list[str],
    event_id: str,
    filename: str | None,
    data: bytes,
    content_type: str,
    resume: bool = False,
) -> tuple[dict, bool]:
    """Создание пина по сценарию ТР с идемпотентным ключом event_id.

    Возвращает (пин, created). created=False — повтор уже выполненного запроса.
    1. pin_requests(user_id, event_id) -> pin_id, status=in_progress (коммит).
       Если запись уже есть: done — вернуть сохранённый пин; in_progress моложе
       30 с — Conflict («обрабатывается»); старше — продолжить с тем же pin_id.
       resume=True — это повтор того же HTTP-запроса после отказа мастера:
       продолжаем сразу, не дожидаясь 30 с.
    2. Файл в MinIO по ключу pins/<pinId>/<filename> (повтор перезаписывает).
    3. Одной транзакцией узла автора: pins, author_pins, outbox, status=done.
    """
    event_id = parse_uuid(event_id, "event_id")
    title = (title or "").strip()
    if not title:
        raise BadRequest("название пина не может быть пустым")
    if not data:
        raise BadRequest("файл изображения пуст")
    ensure_user(author_id)

    pin_id = str(uuid.uuid4())
    with get_conn(author_id) as conn:
        row = conn.execute(
            "INSERT INTO pin_requests (user_id, event_id, pin_id) VALUES (%s, %s::uuid, %s::uuid) "
            "ON CONFLICT DO NOTHING RETURNING pin_id",
            (author_id, event_id, pin_id),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT pin_id, status FROM pin_requests WHERE user_id = %s AND event_id = %s::uuid",
                (author_id, event_id),
            ).fetchone()
            pin_id = str(existing["pin_id"])
            if existing["status"] == "done":
                done = True
            else:
                done = False
                taken = conn.execute(
                    "UPDATE pin_requests SET updated_at = now() "
                    "WHERE user_id = %s AND event_id = %s::uuid AND status = 'in_progress' "
                    "AND updated_at < now() - make_interval(secs => %s::double precision) RETURNING pin_id",
                    (author_id, event_id, 0 if resume else config.PIN_REQUEST_STALE_SECONDS),
                ).fetchone()
                if taken is None:
                    raise Conflict("Запрос с этим event_id уже обрабатывается, повторите позже")
        else:
            done = False

    if done:
        pin = get_pin(author_id, pin_id)
        if pin is None:  # не должно случаться: done ставится в одной транзакции с pins
            raise Conflict("Пин в несогласованном состоянии")
        return pin, False

    image_key = f"pins/{pin_id}/{_safe_filename(filename)}"
    clean_tags = list(dict.fromkeys(t.strip().lower() for t in tags if t and t.strip()))
    try:
        storage.upload_file(image_key, data, content_type or "application/octet-stream")
        with get_conn(author_id) as conn:
            created = conn.execute(
                "INSERT INTO pins (pin_id, author_id, title, description, tags, image_key) "
                "VALUES (%s::uuid, %s, %s, %s, %s::text[], %s) ON CONFLICT DO NOTHING "
                "RETURNING created_at",
                (pin_id, author_id, title, description or "", clean_tags, image_key),
            ).fetchone()
            created_at = created["created_at"] if created else None
            conn.execute(
                "INSERT INTO author_pins (author_id, pin_id, image_key, title, created_at) "
                "SELECT author_id, pin_id, image_key, title, created_at FROM pins "
                "WHERE pin_id = %s::uuid ON CONFLICT DO NOTHING",
                (pin_id,),
            )
            event = build_event(
                event_id=event_id, event_type=PIN_CREATED, user_id=author_id, pin_id=pin_id,
                author_id=author_id, shard=shard_of(author_id), tags=clean_tags,
                timestamp=created_at,
            )
            conn.execute(
                "INSERT INTO outbox (event_id, pin_id, kind, payload) "
                "VALUES (%s::uuid, %s::uuid, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                (event_id, pin_id, PIN_CREATED, _json(event)),
            )
            conn.execute(
                "UPDATE pin_requests SET status = 'done', updated_at = now() "
                "WHERE user_id = %s AND event_id = %s::uuid",
                (author_id, event_id),
            )
    except Exception:
        # сбой здесь: разрешаем клиенту повторить сразу, а не через 30 с
        try:
            with get_conn(author_id) as conn:
                conn.execute(
                    "UPDATE pin_requests SET updated_at = now() - interval '1 hour' "
                    "WHERE user_id = %s AND event_id = %s::uuid AND status = 'in_progress'",
                    (author_id, event_id),
                )
        except Exception:
            log.warning("не удалось освободить запись идемпотентности", exc_info=True)
        raise

    pin = get_pin(author_id, pin_id)
    return pin, True


def _json(value) -> str:
    import json

    def default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        return str(o)

    return json.dumps(value, ensure_ascii=False, default=default)
