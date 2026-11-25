import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import feed.auth as auth
import feed.db.connection as fdb
import feed.profiles as profiles
import feed.db.rebalance as rebalance
from scripts import seed
from feed.db.connection import get_conn, get_conn_by_dsn

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("db")]

PW = "correct-horse-1"


def new_login() -> str:
    return "t" + uuid.uuid4().hex[:12]


def add_pin(author: str, title: str, created_at: datetime, tags=("cats",)) -> str:
    pin_id = str(uuid.uuid4())
    seed.insert_pin(pin_id, author, title, "", list(tags), f"pins/{pin_id}/a.jpg", created_at)
    return pin_id


def add_interaction(pin_id: str, user: str, kind: str) -> None:
    with get_conn(pin_id) as conn:
        conn.execute(
            "INSERT INTO interactions (event_id, pin_id, user_id, kind) "
            "VALUES (%s::uuid, %s::uuid, %s, %s)",
            (str(uuid.uuid4()), pin_id, user, kind),
        )


def test_register_then_session_is_valid_and_logout_kills_it():
    uid = new_login()
    session = auth.register(uid, PW, "Тест")
    assert session.token.startswith(uid + ".")
    assert auth.verify_session(session.token) == uid
    assert auth.logout(session.token) is True
    assert auth.verify_session(session.token) is None
    assert auth.logout(session.token) is False


def test_session_table_holds_hash_not_the_token():
    uid = new_login()
    session = auth.register(uid, PW)
    rnd = session.token.split(".", 1)[1]
    with get_conn(uid) as conn:
        hashes = [r["token_hash"] for r in conn.execute(
            "SELECT token_hash FROM sessions WHERE user_id = %s", (uid,)).fetchall()]
    assert hashes and rnd not in hashes


def test_duplicate_registration_is_rejected_and_does_not_overwrite():
    uid = new_login()
    auth.register(uid, PW, "Первый")
    with pytest.raises(auth.LoginTaken):
        auth.register(uid, "another-password-2", "Второй")
    auth.login(uid, PW)
    with pytest.raises(auth.InvalidCredentials):
        auth.login(uid, "another-password-2")
    assert profiles.get_own_profile(uid)["display_name"] == "Первый"


def test_concurrent_registration_of_same_login_yields_exactly_one_winner():
    uid = new_login()
    results = []

    def attempt():
        try:
            auth.register(uid, PW)
            results.append("ok")
        except auth.LoginTaken:
            results.append("taken")

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["ok"] + ["taken"] * 5


def test_login_checks_password():
    uid = new_login()
    auth.register(uid, PW)
    assert auth.verify_session(auth.login(uid, PW).token) == uid
    with pytest.raises(auth.InvalidCredentials):
        auth.login(uid, "wrong-password-9")
    with pytest.raises(auth.InvalidCredentials):
        auth.login(new_login(), PW)


def test_tampered_tokens_are_rejected():
    alice, bob = new_login(), new_login()
    sa, sb = auth.register(alice, PW), auth.register(bob, PW)
    alice_rnd = sa.token.split(".", 1)[1]
    assert auth.verify_session(f"{bob}.{alice_rnd}") is None
    assert auth.verify_session(f"{alice}.{'x' * 43}") is None
    assert auth.verify_session(sb.token) == bob


def test_expired_session_is_rejected():
    uid = new_login()
    auth.ensure_profile(uid)
    session = auth.issue_session(uid, ttl_seconds=2)
    assert auth.verify_session(session.token) == uid
    time.sleep(2.3)
    assert auth.verify_session(session.token) is None


def test_ensure_profile_creates_once_and_allows_session_without_password():
    uid = new_login()
    assert auth.ensure_profile(uid, "Из внешнего сервиса", "http://a/b.png") is True
    assert auth.ensure_profile(uid, "Другое имя") is False
    own = profiles.get_own_profile(uid)
    assert own["display_name"] == "Из внешнего сервиса" and own["avatar_url"] == "http://a/b.png"
    assert auth.verify_session(auth.issue_session(uid).token) == uid
    with pytest.raises(auth.InvalidCredentials):
        auth.login(uid, PW)


def test_issue_session_for_unknown_user_fails():
    with pytest.raises(auth.UnknownUser):
        auth.issue_session(new_login())


def test_user_row_lives_only_on_the_node_chosen_by_the_ring():
    if len(fdb.node_ids()) < 2:
        pytest.skip("нужно минимум 2 узла")
    uid = new_login()
    auth.register(uid, PW)
    owner = fdb.node_for(uid)
    for node in fdb.node_ids():
        with get_conn_by_dsn(fdb.dsn_for_node(node)) as conn:
            n = conn.execute("SELECT count(*) AS n FROM users WHERE user_id = %s", (uid,)).fetchone()["n"]
        assert n == (1 if node == owner else 0), node


def test_publications_are_paged_newest_first_without_gaps_or_repeats():
    uid = new_login()
    auth.ensure_profile(uid)
    base = datetime.now(timezone.utc)
    ids = [add_pin(uid, f"pin {i}", base - timedelta(minutes=i)) for i in range(5)]

    seen, cursor, pages = [], None, 0
    while True:
        page = profiles.list_publications(uid, limit=2, cursor=cursor)
        seen += [i["pin_id"] for i in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages == 3
    assert seen == ids


def test_publications_with_identical_timestamps_still_page_correctly():
    uid = new_login()
    auth.ensure_profile(uid)
    same = datetime.now(timezone.utc)
    ids = {add_pin(uid, f"same {i}", same) for i in range(5)}
    seen, cursor = [], None
    while True:
        page = profiles.list_publications(uid, limit=2, cursor=cursor)
        seen += [i["pin_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 5 and set(seen) == ids


def test_bad_cursor_is_a_value_error():
    with pytest.raises(ValueError):
        profiles.list_publications(new_login(), cursor="not-a-cursor")


def test_empty_publications():
    assert profiles.list_publications(new_login()) == {"items": [], "next_cursor": None}


def test_own_profile_counts_pins_and_likes_across_nodes():
    uid, fan = new_login(), new_login()
    auth.ensure_profile(uid)
    now = datetime.now(timezone.utc)
    pins = [add_pin(uid, f"p{i}", now) for i in range(4)]
    for pin in pins[:3]:
        add_interaction(pin, fan, "like")
    add_interaction(pins[0], fan, "view")
    stranger = new_login()
    auth.ensure_profile(stranger)
    foreign = add_pin(stranger, "чужой", now)
    add_interaction(foreign, fan, "like")

    own = profiles.get_own_profile(uid)
    assert own["pins_count"] == 4
    assert own["likes_count"] == 3
    assert own["likes_count_partial"] is False
    if len(fdb.node_ids()) > 1:
        assert len({fdb.node_for(p) for p in pins}) > 1


def test_likes_count_flags_unreachable_node(monkeypatch):
    uid = new_login()
    auth.ensure_profile(uid)
    dead = "postgresql://postgres@127.0.0.1:1/none"
    monkeypatch.setattr(profiles, "all_node_dsns", lambda: fdb.all_node_dsns() + [dead])
    own = profiles.get_own_profile(uid)
    assert own["likes_count"] == 0 and own["likes_count_partial"] is True


def test_public_profile_exposes_only_public_fields_and_first_page():
    uid = new_login()
    auth.ensure_profile(uid, "Автор", "http://a/b.png")
    now = datetime.now(timezone.utc)
    for i in range(3):
        add_pin(uid, f"pub {i}", now - timedelta(minutes=i))
    pub = profiles.get_public_profile(uid, pins_limit=2)
    assert set(pub) == {"user_id", "display_name", "avatar_url", "pins", "next_cursor"}
    assert pub["display_name"] == "Автор" and len(pub["pins"]) == 2 and pub["next_cursor"]
    assert set(pub["pins"][0]) == {"pin_id", "image_key", "title", "created_at"}


def test_unknown_user_has_no_profile():
    ghost = new_login()
    assert profiles.get_own_profile(ghost) is None
    assert profiles.get_public_profile(ghost) is None


def test_seed_is_idempotent_and_users_can_log_in():
    first = seed.seed(pins=12)
    seed.seed(pins=12)
    assert first == {"users": 4, "pins": 12}
    total_pins = total_index = 0
    for dsn in fdb.all_node_dsns():
        with get_conn_by_dsn(dsn) as conn:
            total_pins += conn.execute(
                "SELECT count(*) AS n FROM pins WHERE starts_with(author_id, 'seed_')").fetchone()["n"]
            total_index += conn.execute(
                "SELECT count(*) AS n FROM author_pins WHERE starts_with(author_id, 'seed_')").fetchone()["n"]
    assert total_pins >= 12 and total_pins == total_index
    assert auth.verify_session(auth.login("seed_alice", seed.SEED_PASSWORD).token) == "seed_alice"
    assert profiles.get_own_profile("seed_alice")["pins_count"] >= 3


def test_rebalance_moves_only_changed_keys_and_everything_stays_reachable(monkeypatch):
    if not os.environ.get("FEED_DSN_N4"):
        pytest.skip("задайте FEED_DSN_N4: пустая база со schema.sql для 4-го узла")
    old_nodes = fdb.node_ids()
    new_nodes = old_nodes + ["N4"]
    old_env = os.environ.get("FEED_NODES", "N1")

    users, tokens, pin_ids = [], {}, []
    now = datetime.now(timezone.utc)
    for _ in range(12):
        uid = new_login()
        tokens[uid] = auth.register(uid, PW).token
        users.append(uid)
        for i in range(2):
            pin_ids.append(add_pin(uid, f"mig {i}", now, tags=("cats", "travel")))
    fan = new_login()
    auth.ensure_profile(fan)
    for pin in pin_ids[::2]:
        add_interaction(pin, fan, "like")
    payload = {"kind": "pin_created", "tags": ["cats", "travel"], "n": [1, 2], "s": "it's"}
    outbox = {}
    for pin in pin_ids:
        outbox[pin] = str(uuid.uuid4())
        with get_conn(pin) as conn:
            conn.execute(
                "INSERT INTO outbox (event_id, pin_id, kind, payload) "
                "VALUES (%s::uuid, %s::uuid, 'pin_created', %s::jsonb)",
                (outbox[pin], pin, json.dumps(payload)),
            )
    for uid, pin in zip(users, pin_ids[::2]):
        with get_conn(uid) as conn:
            conn.execute(
                "INSERT INTO pin_requests (user_id, event_id, pin_id, status) "
                "VALUES (%s, %s::uuid, %s::uuid, 'done')", (uid, str(uuid.uuid4()), pin))
    my_keys = set(users) | set(pin_ids)

    new_ring = fdb.build_ring(new_nodes)
    expected_moving = {k for k in my_keys if new_ring.get_node(k) != fdb.node_for(k)}
    assert expected_moving, "ни один ключ не меняет владельца: тест ничего не проверяет"

    only = lambda k: k in my_keys
    dry = rebalance.rebalance(old_nodes, new_nodes, dry_run=True, only_keys=only)
    assert dry.keys_moved == len(expected_moving)
    assert fdb.node_for(users[0]) == fdb.node_for(users[0])
    assert all(fdb.node_for(k) == old for k, old in [(u, fdb.node_for(u)) for u in users])

    try:
        report = rebalance.rebalance(old_nodes, new_nodes, only_keys=only)
        assert report.keys_moved == len(expected_moving)

        monkeypatch.setenv("FEED_NODES", ",".join(new_nodes))
        fdb.reload_config()
        for uid in users:
            assert auth.verify_session(tokens[uid]) == uid
            own = profiles.get_own_profile(uid)
            assert own["pins_count"] == 2 and not own["likes_count_partial"]
            assert len(profiles.list_publications(uid)["items"]) == 2
        assert sum(profiles.get_own_profile(u)["likes_count"] for u in users) == len(pin_ids[::2])
        for pin in pin_ids:
            with get_conn(pin) as conn:
                row = conn.execute("SELECT payload FROM outbox WHERE event_id = %s::uuid",
                                   (outbox[pin],)).fetchone()
            assert row is not None and row["payload"] == payload
        for uid in users:
            with get_conn(uid) as conn:
                n = conn.execute("SELECT count(*) AS n FROM pin_requests WHERE user_id = %s",
                                 (uid,)).fetchone()["n"]
            assert n == 1

        for key in my_keys:
            holders = []
            for node in new_nodes:
                with get_conn_by_dsn(fdb.dsn_for_node(node)) as conn:
                    col, cast = ("user_id", "text") if key in users else ("pin_id", "uuid")
                    table = "users" if key in users else "pins"
                    n = conn.execute(
                        f"SELECT count(*) AS n FROM {table} WHERE {col} = %s::{cast}", (key,)
                    ).fetchone()["n"]
                if n:
                    holders.append(node)
            assert holders == [fdb.node_for(key)], key

        again = rebalance.rebalance(new_nodes, new_nodes, only_keys=only, dry_run=True)
        assert again.keys_moved == 0
    finally:
        rebalance.rebalance(new_nodes, old_nodes, only_keys=only)
        monkeypatch.setenv("FEED_NODES", old_env)
        fdb.reload_config()

    for uid in users:
        assert auth.verify_session(tokens[uid]) == uid
        assert profiles.get_own_profile(uid)["pins_count"] == 2
