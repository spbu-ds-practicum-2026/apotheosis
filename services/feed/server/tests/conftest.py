"""Тесты сервиса Feed на настоящих узлах PostgreSQL.

Запуск (из services/feed): FEED_E2E=1 FEED_NODES=N1,N2,N3 FEED_DSN_N1=... pytest server/tests
Узлы — пустые базы со схемой feed/db/schema.sql. Без FEED_E2E тесты пропускаются.
Если psycopg не установлен, используется тестовый клиент tests/pgwire.py.
"""
import os
import sys
from pathlib import Path

import pytest

FEED_ROOT = Path(__file__).resolve().parents[2]
for p in (FEED_ROOT, FEED_ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import feed.db.connection as fdb  # noqa: E402


def pytest_collection_modifyitems(config, items):
    if os.environ.get("FEED_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="задайте FEED_E2E=1, FEED_NODES и FEED_DSN_<узел>")
    here = Path(__file__).resolve().parent
    for item in items:
        if here in Path(str(item.fspath)).resolve().parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def db():
    try:
        import psycopg  # noqa: F401
        import psycopg_pool  # noqa: F401
    except ImportError:
        import pgwire

        fdb.set_connection_factory(pgwire.connection)
    fdb.reload_config()
    from server import repository

    repository.apply_extra_schema()
    yield
    fdb.set_connection_factory(None)


class FakeStorage:
    def __init__(self, fail_times: int = 0):
        self.objects: dict[str, bytes] = {}
        self.fail_times = fail_times

    def upload_file(self, key, data, content_type):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("MinIO недоступен")
        self.objects[key] = data

    def get_presigned_url(self, key):
        return f"http://minio.test/{key}"


@pytest.fixture
def storage():
    return FakeStorage()
