import contextlib

import pytest

import feed.db.connection as fdb
import feed.db.rebalance as rebalance
from feed.db.connection import ConfigError


@pytest.fixture(autouse=True)
def no_db():
    @contextlib.contextmanager
    def forbidden(dsn):
        raise AssertionError("к БД обращаться не должны")
        yield

    fdb.set_connection_factory(forbidden)
    yield
    fdb.set_connection_factory(None)


def test_refuses_when_two_nodes_share_a_database():
    with pytest.raises(ConfigError):
        rebalance.rebalance(["N1", "N2"], ["N1", "N2", "N3"],
                            dsns={"N1": "a", "N2": "b", "N3": "a"})


def test_refuses_when_a_node_has_no_dsn():
    with pytest.raises(ConfigError):
        rebalance.rebalance(["N1", "N2"], ["N1", "N2", "N3"], dsns={"N1": "a", "N2": "b"})


def test_cli_reports_config_errors_with_exit_code_1(monkeypatch, capsys):
    for name in ("FEED_DSN_N1", "FEED_DSN_N2", "FEED_DSN_N3", "FEED_DB_DSN"):
        monkeypatch.delenv(name, raising=False)
    assert rebalance.main(["--old", "N1,N2", "--new", "N1,N2,N3"]) == 1
    assert "Ошибка" in capsys.readouterr().err


def test_groups_list_parents_before_children():
    tables = {g.name: [t.name for t in g.tables] for g in rebalance.GROUPS}
    order = tables["author"]
    assert order[0] == "users"
    assert order.index("users") < order.index("sessions") < order.index("author_pins")
    assert order.index("pins") < order.index("interactions")
