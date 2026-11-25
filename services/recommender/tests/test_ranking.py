import uuid

import pytest

from schemas import Event
import ranking as ranking
from store import RecommenderStore


@pytest.fixture
def store():
    return RecommenderStore.from_file()


def tags_of(store, items):
    return [set(store.get_pin(it.pin_id).tags) for it in items]


def like(store, user, pin_id, event_id=None):
    pin = store.get_pin(pin_id)
    return store.apply(Event(event_id=event_id or str(uuid.uuid4()), type="like",
                             user_id=user, pin_id=pin_id, author_id=pin.author_id))


def test_personal_top_matches_interests(store):
    items, _ = ranking.FeedSessions(store).get_page("alice", None, 3)
    assert all("nature" in t for t in tags_of(store, items))


def test_score_is_sum_of_tag_weights(store):
    interests = store.user_interests("alice")
    items, _ = ranking.FeedSessions(store).get_page("alice", None, 10)
    for it in items:
        assert it.score == pytest.approx(ranking.score_pin(store.get_pin(it.pin_id).tags, interests), abs=1e-3)


def test_new_user_gets_popular(store):
    assert not store.has_history("newbie")
    items, _ = ranking.FeedSessions(store).get_page("newbie", None, 10)
    non_fresh = [it for it in items if store.view_count[it.pin_id] >= ranking.FRESH_VIEW_THRESHOLD]
    pops = [store.popularity(it.pin_id) for it in non_fresh]
    assert pops == sorted(pops, reverse=True)
    best = max(store.popularity(p.pin_id) for p in store.all_pins())
    assert pops[0] == best


def test_fresh_pin_gets_quota(store):
    items, _ = ranking.FeedSessions(store).get_page("newbie", None, 10)
    fresh_positions = [i for i, it in enumerate(items) if store.view_count[it.pin_id] < ranking.FRESH_VIEW_THRESHOLD]
    assert 4 in fresh_positions and 9 in fresh_positions


def test_own_and_liked_pins_excluded(store):
    items, _ = ranking.FeedSessions(store).get_page("polina", None, 50)
    assert all(store.get_pin(it.pin_id).author_id != "polina" for it in items)
    items, _ = ranking.FeedSessions(store).get_page("alice", None, 50)
    assert not (store.liked["alice"] & {it.pin_id for it in items})


def test_like_changes_next_portion(store):
    """DoD: после лайка в следующих порциях — пины с похожими тегами."""
    sessions = ranking.FeedSessions(store)
    first, cursor = sessions.get_page("newbie", None, 10)
    food = next(p for p in store.all_pins() if "food" in p.tags and p.pin_id not in {i.pin_id for i in first})
    like(store, "newbie", food.pin_id)
    second, _ = sessions.get_page("newbie", cursor, 4)
    assert all("food" in t for t in tags_of(store, second))


def test_no_repeats_within_scroll_and_infinite(store):
    sessions = ranking.FeedSessions(store)
    candidates = len(ranking.rank(store, "bob"))
    seen, cursor = [], None
    while len(seen) < candidates:
        items, cursor = sessions.get_page("bob", cursor, 7)
        seen += [it.pin_id for it in items]
    assert len(seen[:candidates]) == len(set(seen[:candidates]))   # без повторов в одном круге
    items, cursor = sessions.get_page("bob", cursor, 7)
    assert items                                                    # и лента не кончается


def test_same_cursor_returns_same_page(store):
    sessions = ranking.FeedSessions(store)
    _, cursor = sessions.get_page("alice", None, 5)
    a, next_a = sessions.get_page("alice", cursor, 5)
    b, next_b = sessions.get_page("alice", cursor, 5)
    assert a == b and next_a == next_b


def test_foreign_cursor_starts_new_scroll(store):
    sessions = ranking.FeedSessions(store)
    _, cursor = sessions.get_page("alice", None, 5)
    _, bob_cursor = sessions.get_page("bob", cursor, 5)
    assert bob_cursor.split(":")[0] != cursor.split(":")[0]


def test_duplicate_event_not_applied_twice(store):
    pin = next(p for p in store.all_pins() if "space" in p.tags)
    eid = str(uuid.uuid4())
    assert like(store, "newbie", pin.pin_id, eid) is True
    before = (store.user_interests("newbie"), store.like_count[pin.pin_id])
    assert like(store, "newbie", pin.pin_id, eid) is False
    assert (store.user_interests("newbie"), store.like_count[pin.pin_id]) == before


def test_second_like_same_pin_does_not_double_weight(store):
    pin = next(p for p in store.all_pins() if "art" in p.tags)
    like(store, "newbie", pin.pin_id)
    w = store.user_interests("newbie")["art"]
    like(store, "newbie", pin.pin_id)   # другой event_id, тот же пин
    assert store.user_interests("newbie")["art"] == w


def test_pin_created_event_indexes_tags(store):
    pid = str(uuid.uuid4())
    store.apply(Event(event_id=str(uuid.uuid4()), type="pin_created", user_id="sasha",
                      pin_id=pid, author_id="sasha", tags=["nature", "sea"]))
    assert pid in store.pins_by_tag["sea"]
    items, _ = ranking.FeedSessions(store).get_page("alice", None, 5)
    assert pid in {it.pin_id for it in items}   # свежий пин с подходящими тегами — в первой порции
