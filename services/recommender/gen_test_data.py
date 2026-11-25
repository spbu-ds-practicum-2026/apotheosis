import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

NS = uuid.UUID("6f1c2a52-6a0e-4d3b-9a43-6c1f0d5e7a10")
OUT = Path(__file__).resolve().parent / "test_data.json"
FEED_OUT = Path(__file__).resolve().parent.parent / "feed" / "test_pins.json"

AUTHORS = ["polina", "sasha", "seva", "bogdan"]

# тема -> (теги темы, заголовки)
TOPICS = {
    "nature":       (["nature", "landscape"], ["Закат на море", "Горное озеро", "Осенний лес", "Туман в долине", "Водопад", "Поле подсолнухов"]),
    "cats":         (["animals", "cats", "cute"], ["Рыжий кот", "Котёнок в коробке", "Кот на подоконнике", "Спящий кот", "Кот и клубок", "Чёрный кот"]),
    "dogs":         (["animals", "dogs"], ["Корги на пляже", "Хаски в снегу", "Щенок лабрадора", "Такса в свитере", "Собака с мячом"]),
    "food":         (["food", "cooking"], ["Паста карбонара", "Домашняя пицца", "Блины с ягодами", "Рамен", "Чизкейк", "Борщ"]),
    "architecture": (["architecture", "city"], ["Старинный собор", "Небоскрёбы", "Мост ночью", "Улочки Праги", "Дворцовая площадь"]),
    "space":        (["space", "science"], ["Млечный путь", "Туманность Ориона", "Луна в телескоп", "Северное сияние", "Запуск ракеты"]),
    "art":          (["art", "drawing"], ["Акварельный пейзаж", "Скетч портрета", "Граффити", "Масляный натюрморт", "Цифровая иллюстрация"]),
    "travel":       (["travel", "city"], ["Венеция", "Токио ночью", "Стамбул", "Рим", "Барселона", "Казань"]),
}
EXTRA_TAGS = {"nature": "sea", "cats": "pets", "dogs": "pets", "food": "italian",
              "architecture": "history", "space": "night", "art": "painting", "travel": "europe"}


def main() -> None:
    rnd = random.Random(42)
    base = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    pins = []
    i = 0
    for topic, (tags, titles) in TOPICS.items():
        for n, title in enumerate(titles):
            slug = f"{topic}-{n}"
            pin_id = str(uuid.uuid5(NS, slug))
            pin_tags = list(tags) + ([EXTRA_TAGS[topic]] if n % 2 == 0 else [])
            pins.append({
                "pin_id": pin_id,
                "author_id": AUTHORS[i % len(AUTHORS)],
                "image_key": f"pins/{pin_id}/{slug}.jpg",
                "title": title,
                "description": None,
                "tags": pin_tags,
                "created_at": (base + timedelta(hours=7 * i)).isoformat(),
                "_topic": topic,
            })
            i += 1

    by_topic: dict[str, list[dict]] = {}
    for p in pins:
        by_topic.setdefault(p["_topic"], []).append(p)

    # самые новые пины — «свежие», без просмотров (холодный старт пина)
    fresh = {p["pin_id"] for p in sorted(pins, key=lambda p: p["created_at"])[-4:]}

    interactions = []

    def add(user: str, pin: dict, kind: str) -> None:
        interactions.append({
            "event_id": str(uuid.uuid5(NS, f"{user}-{pin['pin_id']}-{kind}-{len(interactions)}")),
            "type": kind,
            "user_id": user,
            "pin_id": pin["pin_id"],
            "author_id": pin["author_id"],
        })

    # фоновые пользователи создают популярность: смотрят случайное, лайкают «свои» темы
    crowd_tastes = {
        "u_anna": ["cats", "food"], "u_ivan": ["space", "nature"], "u_olga": ["cats", "travel"],
        "u_petr": ["food", "architecture"], "u_masha": ["cats", "art"], "u_dima": ["nature", "travel"],
    }
    for user, tastes in crowd_tastes.items():
        for p in pins:
            if p["pin_id"] not in fresh:
                add(user, p, "view")
        for t in tastes:
            for p in rnd.sample(by_topic[t], 3):
                if p["pin_id"] not in fresh:
                    add(user, p, "like")

    # демо-пользователи с явными вкусами для проверки ранжирования
    for p in by_topic["nature"][:3]:
        add("alice", p, "view")
        add("alice", p, "like")
    for p in by_topic["space"][:2]:
        add("alice", p, "view")
    for p in by_topic["dogs"][:3]:
        add("bob", p, "view")
        add("bob", p, "like")
    # newbie — без истории (холодный старт пользователя)

    for p in pins:
        del p["_topic"]
    OUT.write_text(json.dumps({"pins": pins, "interactions": interactions}, ensure_ascii=False, indent=2))
    FEED_OUT.write_text(json.dumps({"pins": pins}, ensure_ascii=False, indent=2))
    print(f"{len(pins)} pins, {len(interactions)} interactions -> {OUT}, {FEED_OUT}")


if __name__ == "__main__":
    main()
