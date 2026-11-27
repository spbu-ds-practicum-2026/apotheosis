"""Повтор записи при отказе мастера узла Feed DB (ТР, сценарий failover, шаг 4).

Если запись не прошла из-за недоступности мастера (соединение оборвано, таймаут,
узел стал репликой и отвечает «read-only»), Feed перечитывает топологию из etcd
и повторяет операцию с экспоненциальной задержкой в пределах общего окна
(FEED_FAILOVER_WINDOW, по умолчанию 30 с). За это время Patroni успевает
повысить реплику. Не успел — клиент получает 503 и повторяет запрос сам.

Повтор безопасен, потому что все записи Feed идемпотентны по event_id: если
первая попытка успела закоммититься (а синхронная реплика её получила), повтор
вернёт результат первой операции, а не применит её второй раз.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, TypeVar

log = logging.getLogger("feed.failover")

WINDOW_SECONDS = float(os.getenv("FEED_FAILOVER_WINDOW", "30"))
FIRST_DELAY = 0.2
MAX_DELAY = 3.0

# SQLSTATE: подключение (08*), остановка сервера (57P*), запись в реплику (25006)
_RETRIABLE_SQLSTATE_PREFIXES = ("08", "57P")
_RETRIABLE_SQLSTATES = {"25006"}
_RETRIABLE_NAMES = {
    "OperationalError", "PoolTimeout", "PoolClosed", "ReadOnlySqlTransaction",
    "AdminShutdown", "CrashShutdown", "CannotConnectNow", "ConnectionTimeout",
}

T = TypeVar("T")


class Unavailable(Exception):
    """Мастер узла так и не стал доступен за окно повторов."""


def is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    state = getattr(exc, "sqlstate", None)
    diag = getattr(exc, "diag", None)
    if state is None and diag is not None:
        state = getattr(diag, "sqlstate", None)
    if state and (state in _RETRIABLE_SQLSTATES or state.startswith(_RETRIABLE_SQLSTATE_PREFIXES)):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _RETRIABLE_NAMES:
            return True
    text = str(exc).lower()
    return "read-only transaction" in text or "соединение закрыто" in text


def with_retry(op: Callable[[int], T], refresh: Callable[[], object],
               window: float = WINDOW_SECONDS) -> T:
    """Выполнить op(attempt); при отказе мастера — перечитать топологию и повторить."""
    deadline = time.monotonic() + window
    delay = FIRST_DELAY
    attempt = 0
    while True:
        try:
            return op(attempt)
        except Exception as exc:
            if not is_retriable(exc):
                raise
            if time.monotonic() + delay > deadline:
                log.error("мастер недоступен %.0f с, сдаюсь: %s", window, exc)
                raise Unavailable(str(exc)) from exc
            log.warning("запись не прошла (%s), попытка %d, повтор через %.1f с",
                        type(exc).__name__, attempt + 1, delay)
            time.sleep(delay)
            try:
                refresh()
            except Exception:
                log.warning("не удалось перечитать топологию", exc_info=True)
            attempt += 1
            delay = min(delay * 2, MAX_DELAY)
