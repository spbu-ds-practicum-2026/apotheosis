from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime

from feed.db.connection import all_node_dsns, get_conn, get_conn_by_dsn

log = logging.getLogger(__name__)

DEFAULT_PAGE, MAX_PAGE = 20, 100


def _iso(value: datetime) -> str:
    return value.isoformat()


def _encode_cursor(created_at: datetime, pin_id: str) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{pin_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at, pin_id = raw.split("|")
        return datetime.fromisoformat(created_at), str(uuid.UUID(pin_id))
    except Exception:
        raise ValueError("некорректный cursor") from None


def _publications(conn, author_id: str, limit: int, cursor: str | None) -> dict:
    limit = max(1, min(int(limit), MAX_PAGE))
    if cursor:
        after_at, after_id = _decode_cursor(cursor)
        rows = conn.execute(
            "SELECT pin_id, image_key, title, created_at FROM author_pins "
            "WHERE author_id = %s AND (created_at, pin_id) < (%s::timestamptz, %s::uuid) "
            "ORDER BY created_at DESC, pin_id DESC LIMIT %s",
            (author_id, after_at, after_id, limit + 1),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT pin_id, image_key, title, created_at FROM author_pins "
            "WHERE author_id = %s ORDER BY created_at DESC, pin_id DESC LIMIT %s",
            (author_id, limit + 1),
        ).fetchall()
    page = rows[:limit]
    items = [
        {
            "pin_id": str(r["pin_id"]),
            "image_key": r["image_key"],
            "title": r["title"],
            "created_at": _iso(r["created_at"]),
        }
        for r in page
    ]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["created_at"], str(last["pin_id"]))
    return {"items": items, "next_cursor": next_cursor}


def list_publications(author_id: str, limit: int = DEFAULT_PAGE, cursor: str | None = None) -> dict:
    with get_conn(author_id) as conn:
        return _publications(conn, author_id, limit, cursor)


def _likes_received(user_id: str) -> tuple[int, bool]:
    total, partial = 0, False
    for dsn in all_node_dsns():
        try:
            with get_conn_by_dsn(dsn) as conn:
                row = conn.execute(
                    "SELECT count(*) AS n FROM interactions i JOIN pins p ON p.pin_id = i.pin_id "
                    "WHERE p.author_id = %s AND i.kind = 'like'",
                    (user_id,),
                ).fetchone()
            total += int(row["n"])
        except Exception:
            log.warning("узел недоступен при подсчёте лайков", exc_info=True)
            partial = True
    return total, partial


def get_own_profile(user_id: str) -> dict | None:
    with get_conn(user_id) as conn:
        user = conn.execute(
            "SELECT user_id, display_name, avatar_url, created_at FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if user is None:
            return None
        pins_count = int(
            conn.execute(
                "SELECT count(*) AS n FROM author_pins WHERE author_id = %s", (user_id,)
            ).fetchone()["n"]
        )
    likes_count, partial = _likes_received(user_id)
    return {
        "user_id": user["user_id"],
        "display_name": user["display_name"],
        "avatar_url": user["avatar_url"],
        "created_at": _iso(user["created_at"]),
        "pins_count": pins_count,
        "likes_count": likes_count,
        "likes_count_partial": partial,
    }


def get_public_profile(user_id: str, pins_limit: int = DEFAULT_PAGE) -> dict | None:
    with get_conn(user_id) as conn:
        user = conn.execute(
            "SELECT user_id, display_name, avatar_url FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
        if user is None:
            return None
        pubs = _publications(conn, user_id, pins_limit, None)
    return {
        "user_id": user["user_id"],
        "display_name": user["display_name"],
        "avatar_url": user["avatar_url"],
        "pins": pubs["items"],
        "next_cursor": pubs["next_cursor"],
    }
