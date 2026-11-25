import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from schemas import Event, PinMetadata

# Вклад взаимодействия в вес интереса по каждому тегу пина
LIKE_WEIGHT = 1.0
VIEW_WEIGHT = 0.1

DEFAULT_DATA_FILE = Path(__file__).resolve().parent / "test_data.json"


class RecommenderStore:
    def __init__(self) -> None:
        self.pins: dict[str, PinMetadata] = {}
        self.pins_by_tag: dict[str, set[str]] = defaultdict(set)
        self.processed_events: set[str] = set()          # дедупликация по event_id
        self.interests: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.liked: dict[str, set[str]] = defaultdict(set)    # user -> pin_id
        self.like_count: dict[str, int] = defaultdict(int)    # pin -> лайки
        self.view_count: dict[str, int] = defaultdict(int)    # pin -> показы
        self.history: list[Event] = []

    @classmethod
    def from_file(cls, path: Path = DEFAULT_DATA_FILE) -> "RecommenderStore":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls()
        for p in raw["pins"]:
            store.add_pin(PinMetadata(**p))
        for it in raw["interactions"]:
            store.apply(Event(**it))
        return store

    def add_pin(self, pin: PinMetadata) -> None:
        self.pins[pin.pin_id] = pin
        for tag in pin.tags:
            self.pins_by_tag[tag].add(pin.pin_id)

    def apply(self, event: Event) -> bool:
        if event.event_id in self.processed_events:
            return False
        self.processed_events.add(event.event_id)
        self.history.append(event)

        if event.type == "pin_created":
            if event.pin_id not in self.pins:
                self.add_pin(PinMetadata(
                    pin_id=event.pin_id, author_id=event.author_id,
                    image_key="", title="", tags=event.tags, created_at=event.occurred_at,
                ))
            return True

        pin = self.pins.get(event.pin_id)
        if event.type == "like":
            if event.pin_id in self.liked[event.user_id]:
                return True  # повторный лайк того же пина (новый event_id) вес не удваивает
            self.liked[event.user_id].add(event.pin_id)
            self.like_count[event.pin_id] += 1
            weight = LIKE_WEIGHT
        else:  # view
            self.view_count[event.pin_id] += 1
            weight = VIEW_WEIGHT

        if pin is not None:
            for tag in pin.tags:
                self.interests[event.user_id][tag] += weight
        return True

    def user_interests(self, user_id: str) -> dict[str, float]:
        return dict(self.interests.get(user_id, {}))

    def has_history(self, user_id: str) -> bool:
        return bool(self.interests.get(user_id))

    def popularity(self, pin_id: str) -> float:
        return self.like_count.get(pin_id, 0) + VIEW_WEIGHT * self.view_count.get(pin_id, 0)

    def get_pin(self, pin_id: str) -> Optional[PinMetadata]:
        return self.pins.get(pin_id)

    def all_pins(self) -> list[PinMetadata]:
        return list(self.pins.values())

    def created_ts(self, pin_id: str) -> float:
        created: datetime = self.pins[pin_id].created_at
        return created.timestamp()
