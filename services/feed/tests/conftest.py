import os

import pytest

import feed.db.connection as fdb


def pytest_collection_modifyitems(config, items):
    if os.environ.get("FEED_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="e2e: задайте FEED_E2E=1, FEED_NODES и FEED_DSN_<узел> (см. README)")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def db():
    try:
        import psycopg
        import psycopg_pool
    except ImportError:
        import pgwire

        fdb.set_connection_factory(pgwire.connection)
    fdb.reload_config()
    yield
    fdb.set_connection_factory(None)
    fdb.close_all()
