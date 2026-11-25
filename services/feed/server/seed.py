"""Демо-данные: пины с картинками в MinIO и несколько лайков для «популярного».

Идёт через обычные create_pin / record_interaction с постоянными event_id,
поэтому повторный запуск (перезапуск контейнера) ничего не дублирует, а
события попадают в outbox и доходят до Recommender так же, как настоящие.
"""
from __future__ import annotations

import logging
import time
import uuid

from server import repository

log = logging.getLogger("feed.seed")

_NS = uuid.UUID("2b0c7f0e-5d7a-4d0e-8f43-0a6b8f3c9e21")

# автор, название, описание, категории, цвет сверху, снизу, акцент
DEMO_PINS = [
    ("alice", "Закат в горах", "Тёплый свет над вершинами", ["горы", "закат", "природа"], "#f97316", "#7c2d12", "#fde047"),
    ("bob", "Ночной Петербург", "Мосты и отражения в Неве", ["город", "архитектура", "ночь"], "#1e3a8a", "#0f172a", "#e2e8f0"),
    ("charlie", "Кофейный минимализм", "Утренний эспрессо на столе", ["кофе", "минимализм"], "#b45309", "#451a03", "#fed7aa"),
    ("diana", "Северное сияние", "Зелёные волны в ночном небе", ["космос", "природа", "ночь"], "#0f172a", "#064e3b", "#34d399"),
    ("elena", "Морской бриз", "Шум волн на пустом пляже", ["море", "природа"], "#0284c7", "#0c4a6e", "#bae6fd"),
    ("frank", "Осенний парк", "Золотые листья на аллее", ["осень", "природа"], "#d97706", "#78350f", "#fef08a"),
    ("grace", "Неоновая улица", "Огни мегаполиса после дождя", ["киберпанк", "ночь", "город"], "#7c3aed", "#1e1b4b", "#f472b6"),
    ("henry", "Снежный склон", "Свежий вельвет с утра", ["снег", "горы", "спорт"], "#38bdf8", "#1e293b", "#ffffff"),
    ("alice", "Горное озеро", "Вода цвета бирюзы", ["горы", "природа"], "#0ea5e9", "#164e63", "#a5f3fc"),
    ("bob", "Старый собор", "Готика в утреннем тумане", ["архитектура", "город"], "#94a3b8", "#334155", "#f8fafc"),
    ("charlie", "Латте-арт", "Сердце из молочной пены", ["кофе", "арт"], "#a16207", "#3f2a0a", "#fef3c7"),
    ("diana", "Млечный путь", "Звёзды над пустыней", ["космос", "ночь"], "#020617", "#1e1b4b", "#c7d2fe"),
    ("elena", "Маяк на скале", "Шторм у северного берега", ["море", "закат"], "#1d4ed8", "#0f172a", "#fbbf24"),
    ("frank", "Рыжий кот", "Кот греется в листве", ["животные", "осень"], "#ea580c", "#431407", "#fdba74"),
    ("grace", "Киберпанк-переулок", "Голограммы и пар из люков", ["киберпанк", "город"], "#db2777", "#2e1065", "#22d3ee"),
    ("henry", "Лыжня в лесу", "Тишина зимнего утра", ["снег", "спорт", "природа"], "#e2e8f0", "#475569", "#38bdf8"),
    ("alice", "Абстракция", "Круги и линии", ["арт", "минимализм"], "#f43f5e", "#4c0519", "#fde68a"),
    ("bob", "Мост в огнях", "Вечерний разводной мост", ["город", "ночь", "архитектура"], "#312e81", "#020617", "#fcd34d"),
    ("diana", "Луна", "Полнолуние над крышами", ["космос", "город"], "#111827", "#030712", "#f9fafb"),
    ("elena", "Дельфины", "Стая у берега на рассвете", ["море", "животные"], "#06b6d4", "#083344", "#ecfeff"),
    ("henry", "Футбол во дворе", "Вечерний матч", ["спорт", "город"], "#16a34a", "#14532d", "#f0fdf4"),
    ("grace", "Граффити", "Стена в промзоне", ["арт", "город"], "#a855f7", "#3b0764", "#facc15"),
    ("frank", "Туман в долине", "Утро в горах", ["природа", "горы", "осень"], "#9ca3af", "#374151", "#fef9c3"),
    ("charlie", "Белая комната", "Только стул и окно", ["минимализм", "архитектура"], "#f5f5f4", "#a8a29e", "#57534e"),
]

# Лайки демо-пользователей, чтобы у новичка была осмысленная «популярная» выдача.
DEMO_LIKES = {
    "demo_anna": [0, 3, 4, 8, 12],
    "demo_ivan": [3, 11, 18, 0],
    "demo_olga": [6, 14, 1, 17, 0],
    "demo_petr": [2, 10, 23, 3],
}


def _event_id(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


def seed(storage) -> int:
    from app.repository import _make_demo_png_bytes  # генератор картинок из app/

    pin_ids: list[tuple[str, str]] = []
    created = 0
    for idx, (author, title, desc, tags, c1, c2, accent) in enumerate(DEMO_PINS):
        png = _make_demo_png_bytes(c1, c2, accent, idx)
        pin, is_new = repository.create_pin(
            storage=storage, author_id=author, title=title, description=desc, tags=tags,
            event_id=_event_id(f"pin-{idx}"), filename=f"demo_{idx}.png", data=png,
            content_type="image/png",
        )
        created += is_new
        pin_ids.append((author, pin["pin_id"]))
    for user, indexes in DEMO_LIKES.items():
        repository.ensure_user(user)
        for i in indexes:
            author, pin_id = pin_ids[i]
            repository.record_interaction("view", user, pin_id, _event_id(f"view-{user}-{i}"), author)
            repository.record_interaction("like", user, pin_id, _event_id(f"like-{user}-{i}"), author)
    return created


def seed_with_retry(storage, attempts: int = 30, delay: float = 2.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            n = seed(storage)
            log.info("демо-данные готовы, новых пинов: %d", n)
            return
        except Exception as exc:
            log.warning("демо-данные: попытка %d не удалась (%s)", attempt, exc)
            time.sleep(delay)
    log.error("демо-данные так и не загружены")
