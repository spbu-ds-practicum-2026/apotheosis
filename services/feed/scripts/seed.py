from __future__ import annotations

import argparse
import random
import uuid
from datetime import datetime, timedelta, timezone

from feed.auth import hash_password
from feed.db.connection import get_conn

SEED_PASSWORD = "password123"
SEED_USERS = {
    "seed_alice": "Алиса",
    "seed_bob": "Боб",
    "seed_carol": "Кэрол",
    "seed_dave": "Дэйв",
}
_NAMESPACE = uuid.UUID("6f1d2a52-0c1e-4c0e-9a57-5a3f2f6b9c11")
_TAGS = ["cats", "dogs", "travel", "food", "architecture", "nature", "art", "cars",
         "mountains", "sea", "city", "night"]
_ADJ = ["Тихий", "Яркий", "Старый", "Зимний", "Летний", "Дальний", "Уютный", "Шумный"]
_NOUN = ["двор", "берег", "мост", "рынок", "лес", "вокзал", "парк", "закат"]


def insert_pin(pin_id: str, author_id: str, title: str, description: str, tags: list[str],
               image_key: str, created_at: datetime) -> None:
    with get_conn(pin_id) as conn:
        conn.execute(
            "INSERT INTO pins (pin_id, author_id, title, description, tags, image_key, created_at) "
            "VALUES (%s::uuid, %s, %s, %s, %s::text[], %s, %s::timestamptz) "
            "ON CONFLICT (pin_id) DO NOTHING",
            (pin_id, author_id, title, description, tags, image_key, created_at),
        )
    with get_conn(author_id) as conn:
        conn.execute(
            "INSERT INTO author_pins (author_id, pin_id, image_key, title, created_at) "
            "VALUES (%s, %s::uuid, %s, %s, %s::timestamptz) ON CONFLICT DO NOTHING",
            (author_id, pin_id, image_key, title, created_at),
        )


def seed(pins: int = 30, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    pw_hash = hash_password(SEED_PASSWORD)
    for user_id, name in SEED_USERS.items():
        with get_conn(user_id) as conn:
            conn.execute(
                "INSERT INTO users (user_id, password_hash, display_name) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id, pw_hash, name),
            )
    rng = random.Random(42)
    authors = list(SEED_USERS)
    for i in range(pins):
        pin_id = str(uuid.uuid5(_NAMESPACE, f"seed-pin-{i}"))
        author = authors[i % len(authors)]
        insert_pin(
            pin_id=pin_id,
            author_id=author,
            title=f"{rng.choice(_ADJ)} {rng.choice(_NOUN)} #{i}",
            description="Тестовый пин",
            tags=rng.sample(_TAGS, rng.choice([2, 3])),
            image_key=f"pins/{pin_id}/test.jpg",
            created_at=now - timedelta(minutes=47 * i),
        )
    return {"users": len(SEED_USERS), "pins": pins}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Тестовые пользователи и пины")
    parser.add_argument("--pins", type=int, default=30)
    print(seed(parser.parse_args().pins))
