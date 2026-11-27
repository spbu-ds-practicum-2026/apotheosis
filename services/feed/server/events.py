"""Формат события Outbox -> Message Broker -> Recommender (docs/api.md, раздел 5)."""
from __future__ import annotations

from datetime import datetime, timezone

PIN_CREATED = "pin_created"
PIN_LIKED = "pin_liked"
PIN_VIEWED = "pin_viewed"

KIND_TO_EVENT = {"like": PIN_LIKED, "view": PIN_VIEWED}


def build_event(
    *,
    event_id: str,
    event_type: str,
    user_id: str,
    pin_id: str,
    author_id: str,
    shard: str,
    tags: list[str],
    timestamp: datetime | None = None,
) -> dict:
    ts = (timestamp or datetime.now(timezone.utc)).isoformat()
    return {
        "event_id": str(event_id),
        "event_type": event_type,
        "user_id": user_id,
        "pin_id": str(pin_id),
        "timestamp": ts,
        "payload": {"author_id": author_id, "shard": shard, "tags": list(tags)},
    }
