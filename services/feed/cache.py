import time
from typing import Any, Optional


class TTLCache:
    def __init__(self, clock=time.monotonic) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._clock = clock

    def get(self, key: str) -> Optional[Any]:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if self._clock() >= expires:
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = (self._clock() + ttl, value)

    def delete_prefix(self, prefix: str) -> None:
        for k in [k for k in self._data if k.startswith(prefix)]:
            del self._data[k]
