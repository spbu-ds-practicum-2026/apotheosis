import collections
import uuid

import pytest

from feed.db.ring import HashRing, normalize_key

NODES3 = ["N1", "N2", "N3"]


def uuids(n):
    return [str(uuid.UUID(int=(i * 2654435761) % (1 << 128))) for i in range(n)]


def test_defaults():
    ring = HashRing(NODES3)
    assert ring.node_ids == ("N1", "N2", "N3")
    assert ring.tokens_per_node == 256


def test_deterministic_across_instances_and_calls():
    a, b = HashRing(NODES3), HashRing(NODES3)
    for key in ["alice", "bob", uuids(1)[0], "x"]:
        assert a.get_node(key) == a.get_node(key) == b.get_node(key)


def test_independent_of_node_order():
    forward, backward = HashRing(["N1", "N2", "N3"]), HashRing(["N3", "N2", "N1"])
    for i in range(2000):
        assert forward.get_node(f"user{i}") == backward.get_node(f"user{i}")


GOLDEN = {
    "alice": "N3",
    "bob": "N2",
    "carol": "N1",
    "dave": "N2",
    "00000000-0000-0000-0000-000000000001": "N2",
    "7c9e6679-7425-40de-944b-e07fc1f90ae7": "N3",
    "pin-1": "N2",
    "user_42": "N3",
}


@pytest.mark.parametrize("key,node", sorted(GOLDEN.items()))
def test_golden_mapping_is_stable(key, node):
    assert HashRing(NODES3).get_node(key) == node


@pytest.mark.parametrize("nodes", [NODES3, NODES3 + ["N4"], NODES3 + ["N4", "N5"]])
def test_load_is_roughly_even(nodes):
    n = 60_000
    ring = HashRing(nodes)
    counts = collections.Counter(ring.get_node(k) for k in uuids(n))
    assert set(counts) == set(nodes)
    ideal = n / len(nodes)
    for node, c in counts.items():
        assert 0.8 * ideal < c < 1.25 * ideal, f"{node}: {c} при идеале {ideal:.0f}"


def test_logins_also_spread_evenly():
    n = 30_000
    ring = HashRing(NODES3)
    counts = collections.Counter(ring.get_node(f"user{i}") for i in range(n))
    assert all(0.8 * (n / 3) < c < 1.25 * (n / 3) for c in counts.values())


def test_adding_a_node_moves_keys_only_to_the_new_node():
    old, new = HashRing(NODES3), HashRing(NODES3 + ["N4"])
    keys = uuids(30_000)
    moved = [k for k in keys if old.get_node(k) != new.get_node(k)]
    assert all(new.get_node(k) == "N4" for k in moved)
    assert 0.2 < len(moved) / len(keys) < 0.33


def test_removing_a_node_moves_only_its_keys():
    full, reduced = HashRing(NODES3 + ["N4"]), HashRing(NODES3)
    for k in uuids(20_000):
        before = full.get_node(k)
        if before != "N4":
            assert reduced.get_node(k) == before


def test_adding_then_removing_a_node_restores_the_original_mapping():
    base, back = HashRing(NODES3), HashRing(HashRing(NODES3 + ["N4"]).node_ids[:3])
    assert all(base.get_node(k) == back.get_node(k) for k in uuids(5000))


def test_wraps_around_the_ring():
    ring = HashRing(["A"], tokens_per_node=1)
    assert {ring.get_node(f"k{i}") for i in range(200)} == {"A"}


def test_uuid_case_does_not_change_the_owner():
    ring = HashRing(NODES3)
    for k in uuids(200):
        assert ring.get_node(k.upper()) == ring.get_node(k)


def test_normalize_key_touches_only_uuids():
    assert normalize_key("7C9E6679-7425-40DE-944B-E07FC1F90AE7") == "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    assert normalize_key("Alice") == "Alice"


@pytest.mark.parametrize("bad", ["", None, 42])
def test_rejects_bad_keys(bad):
    with pytest.raises(ValueError):
        HashRing(NODES3).get_node(bad)


def test_rejects_bad_config():
    with pytest.raises(ValueError):
        HashRing([])
    with pytest.raises(ValueError):
        HashRing(["N1", "N1"])
    with pytest.raises(ValueError):
        HashRing(["N1"], tokens_per_node=0)
