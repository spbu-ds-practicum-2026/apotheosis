import contextlib

import pytest

import feed.auth as auth
import feed.db.connection as fdb


@pytest.fixture(autouse=True)
def no_db():
    @contextlib.contextmanager
    def forbidden(dsn):
        raise AssertionError("к БД обращаться не должны")
        yield

    fdb.set_connection_factory(forbidden)
    yield
    fdb.set_connection_factory(None)


def test_password_hash_is_salted_and_verifies():
    a, b = auth.hash_password("correct horse"), auth.hash_password("correct horse")
    assert a != b and a.startswith("scrypt$")
    assert "correct horse" not in a
    assert auth.verify_password("correct horse", a)
    assert not auth.verify_password("wrong horse", a)


@pytest.mark.parametrize("stored", [None, "", "garbage", "md5$1$2$3$4$5", "scrypt$x$y$z$!$!"])
def test_verify_rejects_missing_or_malformed_hash(stored):
    assert auth.verify_password("whatever1", stored) is False


@pytest.mark.parametrize("ok", ["abc", "a-b_9", "A" * 32, "seed_alice"])
def test_valid_logins(ok):
    assert auth.validate_login(ok) == ok


@pytest.mark.parametrize("bad", ["", "ab", "a" * 33, "a.b", "ab\n", "юзер", "a b", None, 5])
def test_invalid_logins(bad):
    with pytest.raises(auth.InvalidInput):
        auth.validate_login(bad)


def test_register_validates_before_touching_db():
    with pytest.raises(auth.InvalidInput):
        auth.register("a.b", "password1")
    with pytest.raises(auth.InvalidInput):
        auth.register("alice", "short")
    with pytest.raises(auth.InvalidInput):
        auth.register("alice", "password1", display_name="x" * 65)


def test_login_with_malformed_input_is_just_invalid_credentials():
    with pytest.raises(auth.InvalidCredentials):
        auth.login("a.b", "password1")
    with pytest.raises(auth.InvalidCredentials):
        auth.login("alice", "x")


@pytest.mark.parametrize(
    "token",
    [None, 123, "", "abc", "alice.", ".secret", "al.secret", "a b.secret", "alice\n.secret",
     "x" * 300, "юзер.secret"],
)
def test_garbage_tokens_are_rejected_without_db(token):
    assert auth.verify_session(token) is None
    assert auth.logout(token) is False
