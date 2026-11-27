import bisect
import hashlib

from app.config import settings


class HashRing:
    def __init__(self, shards: list[str], virtual_nodes: int = 64) -> None:
        self.virtual_nodes = virtual_nodes
        self._shards: set[str] = set()
        self._ring: list[int] = []
        self._nodes: dict[int, str] = {}
        for shard in shards:
            self.add_shard(shard)

    @staticmethod
    def _hash(key: str) -> int:
        return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)

    def list_shards(self) -> list[str]:
        return sorted(self._shards)

    def add_shard(self, shard: str) -> bool:
        if shard in self._shards:
            return False
        self._shards.add(shard)
        for i in range(self.virtual_nodes):
            token = self._hash(f"{shard}#{i}")
            self._nodes[token] = shard
            bisect.insort(self._ring, token)
        return True

    def remove_shard(self, shard: str) -> bool:
        if shard not in self._shards:
            return False
        self._shards.remove(shard)
        tokens_to_remove = {self._hash(f"{shard}#{i}") for i in range(self.virtual_nodes)}
        for token in tokens_to_remove:
            self._nodes.pop(token, None)
        self._ring = [t for t in self._ring if t not in tokens_to_remove]
        return True

    def get_shard(self, user_id: str) -> str:
        if not self._ring:
            raise RuntimeError("Hash ring is empty")
        key_hash = self._hash(user_id)
        idx = bisect.bisect(self._ring, key_hash)
        if idx == len(self._ring):
            idx = 0
        return self._nodes[self._ring[idx]]

    def add_shard_and_plan_migrations(self, new_shard: str, user_ids: list[str]) -> list[dict]:
        before = {u: self.get_shard(u) for u in user_ids}
        if not self.add_shard(new_shard):
            return []
        migrations = []
        for user_id, old_shard in before.items():
            target_shard = self.get_shard(user_id)
            if target_shard != old_shard:
                migrations.append({"userId": user_id, "from": old_shard, "to": target_shard})
        return migrations


default_ring = HashRing(settings.initial_shards, virtual_nodes=settings.virtual_nodes)
