import fnmatch

import pytest

from feed.cache import POPULAR_KEY, Cache, portion_key


class FakeRedis:
    def __init__(self):
        self.data, self.ttl = {}, {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)

    def scan_iter(self, match):
        return iter([k for k in list(self.data) if fnmatch.fnmatch(k, match)])


class BrokenRedis:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionError("redis недоступен")

        return fail


def test_json_roundtrip_with_ttl():
    r = FakeRedis()
    cache = Cache(client=r)
    portion = {"items": [{"pin_id": "1", "title": "Тихий двор"}], "next": 2}
    assert cache.set_json(portion_key("alice", 1), portion, ttl_seconds=60)
    assert r.ttl["feed:alice:1"] == 60
    assert cache.get_json("feed:alice:1") == portion


def test_miss_returns_none():
    assert Cache(client=FakeRedis()).get_json(POPULAR_KEY) is None


def test_invalidate_user_drops_only_that_users_portions():
    r = FakeRedis()
    cache = Cache(client=r)
    for key in ["feed:alice:1", "feed:alice:2", "feed:bob:1", POPULAR_KEY]:
        cache.set_json(key, {"x": 1})
    assert cache.invalidate_user("alice")
    assert set(r.data) == {"feed:bob:1", POPULAR_KEY}


def test_redis_outage_degrades_instead_of_raising():
    cache = Cache(client=BrokenRedis())
    assert cache.get_json("k") is None
    assert cache.set_json("k", {}) is False
    assert cache.delete("k") is False
    assert cache.invalidate_user("alice") is False


def test_ttl_must_be_positive():
    with pytest.raises(ValueError):
        Cache(client=FakeRedis()).set_json("k", {}, ttl_seconds=0)
