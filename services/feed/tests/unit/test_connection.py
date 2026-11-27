import contextlib
import os

import pytest

import feed.db.connection as fdb
from feed.db.connection import ConfigError


@pytest.fixture
def cfg(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def factory(dsn):
        calls.append(dsn)
        yield object()

    def apply(**env):
        for name in list(os.environ):
            if name.startswith("FEED_DSN_") or name in ("FEED_DB_DSN", "FEED_NODES"):
                monkeypatch.delenv(name)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        fdb.reload_config()
        fdb.set_connection_factory(factory)
        return calls

    yield apply
    monkeypatch.undo()
    fdb.set_connection_factory(None)
    fdb.reload_config()


def three(**extra):
    return {"FEED_NODES": "N1,N2,N3", "FEED_DSN_N1": "dsn-N1", "FEED_DSN_N2": "dsn-N2",
            "FEED_DSN_N3": "dsn-N3", **extra}


def test_default_is_a_single_node_with_default_dsn(cfg):
    calls = cfg()
    with fdb.get_conn("alice"):
        pass
    assert fdb.node_ids() == ["N1"]
    assert calls == [fdb.DEFAULT_DSN]


def test_single_node_can_use_fallback_dsn(cfg):
    calls = cfg(FEED_DB_DSN="postgresql://one/feed")
    for key in ["alice", "bob", "7c9e6679-7425-40de-944b-e07fc1f90ae7"]:
        with fdb.get_conn(key):
            pass
    assert calls == ["postgresql://one/feed"] * 3
    assert fdb.all_node_dsns() == ["postgresql://one/feed"]


def test_key_goes_to_the_node_chosen_by_the_ring(cfg):
    calls = cfg(**three())
    for key, node in [("alice", "N3"), ("bob", "N2"), ("carol", "N1")]:
        with fdb.get_conn(key):
            pass
        assert calls[-1] == f"dsn-{node}"
        assert fdb.node_for(key) == node


def test_pin_id_case_does_not_change_the_node(cfg):
    cfg(**three())
    pin = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    assert fdb.node_for(pin.upper()) == fdb.node_for(pin) == "N3"


def test_changing_node_list_changes_routing_only_for_some_keys(cfg):
    cfg(**three())
    before = {f"user{i}": fdb.node_for(f"user{i}") for i in range(2000)}
    cfg(**three(FEED_NODES="N1,N2,N3,N4", FEED_DSN_N4="dsn-N4"))
    moved = [k for k, n in before.items() if fdb.node_for(k) != n]
    assert 0 < len(moved) < 0.4 * len(before)
    assert all(fdb.node_for(k) == "N4" for k in moved)


def test_all_node_dsns_follow_sorted_node_order(cfg):
    cfg(**three(FEED_NODES="N3,N1,N2"))
    assert fdb.all_node_dsns() == ["dsn-N1", "dsn-N2", "dsn-N3"]


@pytest.mark.parametrize(
    "env",
    [
        {"FEED_NODES": "N1,N2", "FEED_DSN_N1": "a"},
        {"FEED_NODES": "N1,N2", "FEED_DSN_N1": "a", "FEED_DSN_N2": "a"},
        {"FEED_NODES": "N1,N1", "FEED_DSN_N1": "a"},
        {"FEED_NODES": "N 1", "FEED_DSN_N1": "a"},
        {"FEED_NODES": ",", "FEED_DB_DSN": "a"},
        {"FEED_NODES": "N1,N2", "FEED_DB_DSN": "a"},
    ],
)
def test_bad_configuration_is_rejected(cfg, env):
    cfg(**env)
    with pytest.raises(ConfigError):
        fdb.node_for("alice")


def test_empty_key_is_rejected_before_connecting(cfg):
    calls = cfg(FEED_DB_DSN="x")
    with pytest.raises(ValueError):
        with fdb.get_conn(""):
            pass
    assert calls == []
