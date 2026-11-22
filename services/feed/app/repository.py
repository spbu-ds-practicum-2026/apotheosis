import base64
from datetime import datetime, timezone
import io
import uuid
from PIL import Image, ImageDraw

from app.routing import default_ring


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _make_demo_png(top_hex: str, bottom_hex: str, accent_hex: str, variant: int) -> str:
    width, height = 560, 400
    c1 = _hex_to_rgb(top_hex)
    c2 = _hex_to_rgb(bottom_hex)
    accent = _hex_to_rgb(accent_hex)

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    if variant % 4 == 0:
        draw.ellipse([360, 55, 455, 150], fill=accent)
        draw.polygon([(0, height), (180, 185), (350, height)], fill=(35, 30, 45))
        draw.polygon([(220, height), (410, 155), (width, height)], fill=(22, 20, 34))
    elif variant % 4 == 1:
        draw.ellipse([60, 50, 130, 120], fill=accent)
        for x, h in [(40, 210), (110, 160), (190, 240), (280, 140), (360, 200), (450, 175)]:
            draw.rectangle([x, h, x + 65, height], fill=(15, 23, 42))
    elif variant % 4 == 2:
        draw.ellipse([155, 75, 405, 325], fill=accent)
        draw.ellipse([195, 115, 365, 285], fill=c2)
    else:
        for offset in range(0, 180, 36):
            draw.arc([-40, 60 + offset, width + 40, 260 + offset], start=180, end=360, fill=accent, width=10)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class InMemoryFeedRepository:
    def __init__(self) -> None:
        self.pins: dict[str, dict] = {}
        self.pin_order: list[str] = []
        self.pins_by_user: dict[str, list[str]] = {}
        self.like_events: dict[str, dict] = {}
        self.view_events: dict[str, dict] = {}
        self.user_likes: dict[str, set[str]] = {}
        self.outbox: list[dict] = []
        self._seed_demo_pins()

    def _seed_demo_pins(self) -> None:
        samples = [
            ("alice", "Закат в горах", "Тёплый свет над вершинами", ["горы", "закат", "природа"], "#f97316", "#7c2d12", "#fde047"),
            ("bob", "Ночной Петербург", "Мосты и отражения в Неве", ["город", "архитектура", "ночь"], "#1e3a8a", "#0f172a", "#e2e8f0"),
            ("charlie", "Кофейный минимализм", "Утренний эспрессо на столе", ["кофе", "минимализм", "утро"], "#b45309", "#451a03", "#fed7aa"),
            ("diana", "Северное сияние", "Зелёные волны в ночном небе", ["космос", "природа", "ночь"], "#0f172a", "#064e3b", "#34d399"),
            ("elena", "Морской бриз", "Шум волн на пустом пляже", ["море", "лето", "отдых"], "#0284c7", "#0c4a6e", "#bae6fd"),
            ("frank", "Осенний парк", "Золотые листья на аллее", ["осень", "парк", "природа"], "#d97706", "#78350f", "#fef08a"),
            ("grace", "Неоновая улица", "Огни мегаполиса после дождя", ["киберпанк", "ночь", "город"], "#7c3aed", "#1e1b4b", "#f472b6"),
            ("henry", "Снежный склон", "Свежий вельвет с утра", ["снег", "горы", "спорт"], "#38bdf8", "#1e293b", "#ffffff"),
        ]
        now = datetime.now(timezone.utc).isoformat()
        for idx, (author, title, desc, tags, c1, c2, accent) in enumerate(samples):
            pin_id = str(uuid.uuid4())
            shard = default_ring.get_shard(author)
            self.pins[pin_id] = {
                "pinId": pin_id,
                "authorId": author,
                "title": title,
                "description": desc,
                "tags": tags,
                "image_url": _make_demo_png(c1, c2, accent, idx),
                "likes_count": 0,
                "created_at": now,
                "shard": shard,
            }
            self.pin_order.append(pin_id)
            self.pins_by_user.setdefault(author, []).append(pin_id)

    def add_shard(self, shard_id: str) -> dict:
        users = list(self.pins_by_user.keys())
        migrations = default_ring.add_shard_and_plan_migrations(shard_id, users)
        for item in migrations:
            uid = item["userId"]
            new_shard = item["to"]
            for pin_id in self.pins_by_user.get(uid, []):
                self.pins[pin_id]["shard"] = new_shard
        return {"shards": default_ring.list_shards(), "migrated_users": migrations}

    def list_feed(self, user_id: str, cursor: int, limit: int) -> tuple[list[dict], str | None, bool]:
        slice_ids = self.pin_order[cursor : cursor + limit]
        liked_pins = self.user_likes.get(user_id, set())
        items = []
        for pin_id in slice_ids:
            pin = self.pins[pin_id]
            items.append(
                {
                    "pinId": pin["pinId"],
                    "authorId": pin["authorId"],
                    "title": pin["title"],
                    "description": pin["description"],
                    "tags": pin["tags"],
                    "image_url": pin["image_url"],
                    "likes_count": pin["likes_count"],
                    "liked_by_me": pin_id in liked_pins,
                    "created_at": pin["created_at"],
                    "shard": default_ring.get_shard(pin["authorId"]),
                }
            )
        next_pos = cursor + len(slice_ids)
        has_more = next_pos < len(self.pin_order)
        next_cursor = str(next_pos) if has_more else None
        return items, next_cursor, has_more

    def add_like(self, user_id: str, pin_id: str, event_id: str, author_id: str | None = None) -> dict | None:
        if event_id in self.like_events:
            return self.like_events[event_id]
        pin = self.pins.get(pin_id)
        if not pin:
            return None

        shard = default_ring.get_shard(author_id or pin["authorId"])
        user_set = self.user_likes.setdefault(user_id, set())
        if pin_id not in user_set:
            user_set.add(pin_id)
            pin["likes_count"] += 1

        now = datetime.now(timezone.utc).isoformat()
        self.outbox.append(
            {
                "event_id": event_id,
                "event_type": "pin_liked",
                "user_id": user_id,
                "pin_id": pin_id,
                "timestamp": now,
                "payload": {"author_id": pin["authorId"], "shard": shard, "tags": pin["tags"]},
            }
        )
        result = {
            "status": "ok",
            "pinId": pin_id,
            "userId": user_id,
            "event_id": event_id,
            "likes_count": pin["likes_count"],
            "shard": shard,
        }
        self.like_events[event_id] = result
        return result

    def add_view(self, user_id: str, pin_id: str, event_id: str, author_id: str | None = None) -> dict | None:
        if event_id in self.view_events:
            return self.view_events[event_id]
        pin = self.pins.get(pin_id)
        if not pin:
            return None

        shard = default_ring.get_shard(author_id or pin["authorId"])
        now = datetime.now(timezone.utc).isoformat()
        self.outbox.append(
            {
                "event_id": event_id,
                "event_type": "pin_viewed",
                "user_id": user_id,
                "pin_id": pin_id,
                "timestamp": now,
                "payload": {"author_id": pin["authorId"], "shard": shard, "tags": pin["tags"]},
            }
        )
        result = {"status": "ok", "pinId": pin_id, "userId": user_id, "event_id": event_id, "shard": shard}
        self.view_events[event_id] = result
        return result


repo = InMemoryFeedRepository()
