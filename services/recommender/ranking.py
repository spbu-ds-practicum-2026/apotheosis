import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from schemas import RecommendedItem
from store import RecommenderStore

FRESH_VIEW_THRESHOLD = 5   # пин считается свежим, пока у него меньше 5 показов
FRESH_EVERY = 5            # каждое 5-е место в выдаче — для свежего пина


@dataclass
class Scored:
    pin_id: str
    author_id: str
    score: float
    popularity: float
    created_ts: float

    def sort_key(self):
        # выше оценка -> популярнее -> новее; pin_id — для детерминированности
        return (-self.score, -self.popularity, -self.created_ts, self.pin_id)


def score_pin(tags: list[str], interests: dict[str, float]) -> float:
    return sum(interests.get(t, 0.0) for t in tags)


def rank(store: RecommenderStore, user_id: str, exclude: set[str] = frozenset()) -> list[Scored]:
    interests = store.user_interests(user_id)
    liked = store.liked.get(user_id, set())

    main: list[Scored] = []
    fresh: list[Scored] = []
    for pin in store.all_pins():
        if pin.author_id == user_id or pin.pin_id in liked or pin.pin_id in exclude:
            continue
        s = Scored(
            pin_id=pin.pin_id,
            author_id=pin.author_id,
            score=score_pin(pin.tags, interests),
            popularity=store.popularity(pin.pin_id),
            created_ts=store.created_ts(pin.pin_id),
        )
        if store.view_count.get(pin.pin_id, 0) < FRESH_VIEW_THRESHOLD:
            fresh.append(s)
        else:
            main.append(s)

    main.sort(key=Scored.sort_key)
    fresh.sort(key=Scored.sort_key)
    return _merge_with_fresh_quota(main, fresh)


def _merge_with_fresh_quota(main: list[Scored], fresh: list[Scored]) -> list[Scored]:
    result: list[Scored] = []
    i = j = 0
    while i < len(main) or j < len(fresh):
        quota_slot = len(result) % FRESH_EVERY == FRESH_EVERY - 1
        take_fresh = j < len(fresh) and (
            i >= len(main) or quota_slot or fresh[j].score > main[i].score
        )
        if take_fresh:
            result.append(fresh[j]); j += 1
        else:
            result.append(main[i]); i += 1
    return result


@dataclass
class _ScrollSession:
    user_id: str
    served: set[str] = field(default_factory=set)
    pages: dict[int, list[RecommendedItem]] = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)


class FeedSessions:

    TTL_SECONDS = 30 * 60
    KEEP_PAGES = 3

    def __init__(self, store: RecommenderStore) -> None:
        self.store = store
        self._sessions: dict[str, _ScrollSession] = {}

    def get_page(self, user_id: str, cursor: Optional[str], limit: int) -> tuple[list[RecommendedItem], str]:
        self._gc()
        sid, page = self._parse(cursor)
        sess = self._sessions.get(sid) if sid else None
        if sess is None or sess.user_id != user_id:
            # нет курсора, курсор истёк или чужой — начинаем новую прокрутку
            sid, page = secrets.token_urlsafe(8), 0
            sess = self._sessions[sid] = _ScrollSession(user_id=user_id)
        sess.touched = time.monotonic()

        if page in sess.pages:
            return sess.pages[page], f"{sid}:{page + 1}"

        ranked = rank(self.store, user_id, exclude=sess.served)
        if not ranked and sess.served:
            sess.served.clear()  # всё показали — новый круг, лента бесконечная
            ranked = rank(self.store, user_id)

        items = [RecommendedItem(pin_id=s.pin_id, author_id=s.author_id, score=round(s.score, 4))
                 for s in ranked[:limit]]
        sess.served.update(it.pin_id for it in items)
        sess.pages[page] = items
        for old in [p for p in sess.pages if p <= page - self.KEEP_PAGES]:
            del sess.pages[old]
        return items, f"{sid}:{page + 1}"

    @staticmethod
    def _parse(cursor: Optional[str]) -> tuple[Optional[str], int]:
        if not cursor:
            return None, 0
        sid, _, page = cursor.rpartition(":")
        if not sid or not page.isdigit():
            return None, 0
        return sid, int(page)

    def _gc(self) -> None:
        now = time.monotonic()
        for sid in [s for s, v in self._sessions.items() if now - v.touched > self.TTL_SECONDS]:
            del self._sessions[sid]


def popular(store: RecommenderStore, limit: int) -> list[RecommendedItem]:
    pins = sorted(store.all_pins(),
                  key=lambda p: (-store.popularity(p.pin_id), -store.created_ts(p.pin_id), p.pin_id))
    return [RecommendedItem(pin_id=p.pin_id, author_id=p.author_id, score=store.popularity(p.pin_id))
            for p in pins[:limit]]


def recent(store: RecommenderStore, limit: int) -> list[RecommendedItem]:
    pins = sorted(store.all_pins(), key=lambda p: (-store.created_ts(p.pin_id), p.pin_id))
    return [RecommendedItem(pin_id=p.pin_id, author_id=p.author_id, score=0.0) for p in pins[:limit]]
