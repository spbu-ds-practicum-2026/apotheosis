import threading
import uuid

import pytest

from feed.db.connection import get_conn, node_for
from server import repository as repo
from server.tests.conftest import FakeStorage


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _create(storage, author, event_id=None, tags=("горы", "закат")):
    pin, created = repo.create_pin(
        storage=storage, author_id=author, title="Закат", description="тест",
        tags=list(tags), event_id=event_id or str(uuid.uuid4()), filename="a b/../x.png",
        data=b"png", content_type="image/png",
    )
    return pin, created


def _outbox(author, pin_id):
    with get_conn(author) as conn:
        return conn.execute(
            "SELECT event_id, kind, payload FROM outbox WHERE pin_id = %s::uuid ORDER BY created_at",
            (pin_id,),
        ).fetchall()


def test_create_pin_all_on_author_shard(db, storage):
    author = _uid("author")
    pin, created = _create(storage, author)
    assert created
    assert pin["author_id"] == author and pin["tags"] == ["горы", "закат"]
    assert pin["image_key"] == f"pins/{pin['pin_id']}/x.png"
    assert storage.objects[pin["image_key"]] == b"png"
    with get_conn(author) as conn:
        pub = conn.execute(
            "SELECT count(*) AS n FROM author_pins WHERE author_id = %s", (author,)
        ).fetchone()
        req = conn.execute("SELECT status FROM pin_requests WHERE user_id = %s", (author,)).fetchone()
    assert pub["n"] == 1 and req["status"] == "done"
    events = _outbox(author, pin["pin_id"])
    assert [e["kind"] for e in events] == ["pin_created"]
    payload = events[0]["payload"]
    assert payload["event_type"] == "pin_created"
    assert payload["payload"] == {"author_id": author, "shard": node_for(author), "tags": ["горы", "закат"]}


def test_create_pin_retry_returns_same_pin(db, storage):
    author, ev = _uid("author"), str(uuid.uuid4())
    first, c1 = _create(storage, author, ev)
    second, c2 = _create(storage, author, ev)
    assert c1 and not c2 and first["pin_id"] == second["pin_id"]
    with get_conn(author) as conn:
        n = conn.execute("SELECT count(*) AS n FROM pins WHERE author_id = %s", (author,)).fetchone()["n"]
    assert n == 1 and len(_outbox(author, first["pin_id"])) == 1


def test_create_pin_parallel_same_event_one_pin(db):
    author, ev = _uid("author"), str(uuid.uuid4())
    storage = FakeStorage()
    results, errors = [], []

    def worker():
        try:
            results.append(_create(storage, author, ev)[0]["pin_id"])
        except repo.Conflict:
            errors.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    with get_conn(author) as conn:
        n = conn.execute("SELECT count(*) AS n FROM pins WHERE author_id = %s", (author,)).fetchone()["n"]
    assert n == 1


def test_create_pin_after_storage_failure_retry_succeeds(db):
    author, ev = _uid("author"), str(uuid.uuid4())
    storage = FakeStorage(fail_times=1)
    with pytest.raises(RuntimeError):
        _create(storage, author, ev)
    pin, created = _create(storage, author, ev)   # сразу, без ожидания 30 с
    assert created and storage.objects


def test_like_idempotent_and_once_per_user(db, storage):
    author, liker = _uid("author"), _uid("liker")
    pin, _ = _create(storage, author)
    pid, ev = pin["pin_id"], str(uuid.uuid4())

    r1 = repo.record_interaction("like", liker, pid, ev, author)
    r2 = repo.record_interaction("like", liker, pid, ev, author)          # повтор того же запроса
    r3 = repo.record_interaction("like", liker, pid, str(uuid.uuid4()), author)  # второй лайк
    assert (r1["duplicate"], r2["duplicate"], r3["duplicate"]) == (False, True, True)
    assert r1["likes_count"] == r2["likes_count"] == r3["likes_count"] == 1
    assert r1["liked_by_me"] and r1["shard"] == node_for(author)
    kinds = [e["kind"] for e in _outbox(author, pid)]
    assert kinds == ["pin_created", "pin_liked"]

    other = repo.record_interaction("like", _uid("other"), pid, str(uuid.uuid4()), author)
    assert other["likes_count"] == 2 and other["liked_by_me"]


def test_view_every_new_event_counts(db, storage):
    author, viewer = _uid("author"), _uid("viewer")
    pin, _ = _create(storage, author)
    ev = str(uuid.uuid4())
    repo.record_interaction("view", viewer, pin["pin_id"], ev, author)
    repo.record_interaction("view", viewer, pin["pin_id"], ev, author)
    repo.record_interaction("view", viewer, pin["pin_id"], str(uuid.uuid4()), author)
    kinds = [e["kind"] for e in _outbox(author, pin["pin_id"])]
    assert kinds.count("pin_viewed") == 2


def test_like_without_or_with_wrong_author(db, storage):
    author = _uid("author")
    pin, _ = _create(storage, author)
    r = repo.record_interaction("like", _uid("u"), pin["pin_id"], str(uuid.uuid4()), None)
    assert r["likes_count"] == 1
    r = repo.record_interaction("like", _uid("u"), pin["pin_id"], str(uuid.uuid4()), "nobody_x")
    assert r["likes_count"] == 2
    with pytest.raises(repo.NotFound):
        repo.record_interaction("like", _uid("u"), str(uuid.uuid4()), str(uuid.uuid4()), author)


def test_event_id_reused_for_other_action_conflicts(db, storage):
    author, user = _uid("author"), _uid("u")
    pin, _ = _create(storage, author)
    ev = str(uuid.uuid4())
    repo.record_interaction("view", user, pin["pin_id"], ev, author)
    with pytest.raises(repo.Conflict):
        repo.record_interaction("like", user, pin["pin_id"], ev, author)


def test_bad_ids_rejected(db):
    with pytest.raises(repo.BadRequest):
        repo.record_interaction("like", "user_a", "not-a-uuid", str(uuid.uuid4()), "a")
    with pytest.raises(repo.BadRequest):
        repo.record_interaction("like", "user_a", str(uuid.uuid4()), "nope", "a")


def test_read_helpers(db, storage):
    author, user = _uid("author"), _uid("u")
    p1, _ = _create(storage, author)
    p2, _ = _create(storage, _uid("author"))
    repo.record_interaction("like", user, p1["pin_id"], str(uuid.uuid4()), author)
    items = [(author, p1["pin_id"]), (p2["author_id"], p2["pin_id"])]
    got = repo.get_pins(items)
    assert set(got) == {p1["pin_id"], p2["pin_id"]}
    stats = repo.like_stats(user, items)
    assert stats[p1["pin_id"]] == (1, True) and p2["pin_id"] not in stats
    recent = repo.recent_pins(500)
    ids = [p["pin_id"] for p in recent]
    assert p1["pin_id"] in ids and p2["pin_id"] in ids
    assert ids.index(p2["pin_id"]) < ids.index(p1["pin_id"])   # новые первыми
