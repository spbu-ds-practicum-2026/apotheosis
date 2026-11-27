import json
from pathlib import Path
from typing import Optional

from schemas import PinMetadata

DEFAULT_DATA_FILE = Path(__file__).resolve().parent / "test_pins.json"


class InMemoryPinStore:
    def __init__(self, pins: Optional[list[PinMetadata]] = None) -> None:
        self._pins: dict[str, PinMetadata] = {p.pin_id: p for p in (pins or [])}

    @classmethod
    def from_file(cls, path: Path = DEFAULT_DATA_FILE) -> "InMemoryPinStore":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([PinMetadata(**p) for p in raw["pins"]])

    def add(self, pin: PinMetadata) -> None:
        self._pins[pin.pin_id] = pin

    def get(self, author_id: str, pin_id: str) -> Optional[PinMetadata]:
        pin = self._pins.get(pin_id)
        return pin if pin and pin.author_id == author_id else None

    def get_many(self, items: list[tuple[str, str]]) -> list[PinMetadata]:
        # порядок сохраняется (он задан ранжированием); пропавшие пины пропускаются
        return [p for a, pid in items if (p := self.get(a, pid)) is not None]

    def recent(self, limit: int) -> list[PinMetadata]:
        return sorted(self._pins.values(), key=lambda p: p.created_at, reverse=True)[:limit]

    def by_author(self, author_id: str) -> list[PinMetadata]:
        return sorted((p for p in self._pins.values() if p.author_id == author_id),
                      key=lambda p: p.created_at, reverse=True)
