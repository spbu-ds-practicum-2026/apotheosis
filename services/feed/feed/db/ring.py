from __future__ import annotations

import hashlib
import re
from bisect import bisect_left
from typing import Iterable

DEFAULT_TOKENS_PER_NODE = 256

_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def hash64(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def normalize_key(key: str) -> str:
    return key.lower() if _UUID_RE.fullmatch(key) else key


class HashRing:
    def __init__(
        self, node_ids: Iterable[str], tokens_per_node: int = DEFAULT_TOKENS_PER_NODE
    ) -> None:
        nodes = list(node_ids)
        if not nodes:
            raise ValueError("нужен хотя бы один узел")
        if len(set(nodes)) != len(nodes):
            raise ValueError("идентификаторы узлов должны быть уникальны")
        if tokens_per_node < 1:
            raise ValueError("tokens_per_node должен быть >= 1")

        self._node_ids = tuple(sorted(nodes))
        self._tokens_per_node = tokens_per_node

        points = sorted(
            (hash64(f"{node}#{i}"), node)
            for node in self._node_ids
            for i in range(tokens_per_node)
        )
        self._positions = [p for p, _ in points]
        self._owners = [n for _, n in points]

    @property
    def node_ids(self) -> tuple[str, ...]:
        return self._node_ids

    @property
    def tokens_per_node(self) -> int:
        return self._tokens_per_node

    def get_node(self, key: str) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("ключ шардирования должен быть непустой строкой")
        idx = bisect_left(self._positions, hash64(normalize_key(key)))
        if idx == len(self._positions):
            idx = 0
        return self._owners[idx]
