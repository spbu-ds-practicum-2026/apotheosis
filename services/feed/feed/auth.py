from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime

from feed.db.connection import get_conn

LOGIN_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")
MIN_PASSWORD, MAX_PASSWORD = 8, 128
SESSION_TTL_SECONDS = int(os.environ.get("FEED_SESSION_TTL_SECONDS", 7 * 24 * 3600))

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class AuthError(Exception):
    pass


class InvalidInput(AuthError):
    pass


class LoginTaken(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


class UnknownUser(AuthError):
    pass


@dataclass(frozen=True)
class Session:
    user_id: str
    token: str
    expires_at: datetime


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


_DUMMY_HASH = hash_password("dummy-password-for-constant-time")


def verify_password(password: str, stored: str | None) -> bool:
    candidate = stored if stored else _DUMMY_HASH
    ok = False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = candidate.split("$")
        if scheme != "scrypt":
            raise ValueError(scheme)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), maxmem=_SCRYPT_MAXMEM,
        )
        ok = hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        ok = False
    return ok and stored is not None


def validate_login(login: object) -> str:
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        raise InvalidInput("логин: 3-32 символа, только латиница, цифры, _ и -")
    return login


def _validate_password(password: object) -> str:
    if not isinstance(password, str) or not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise InvalidInput(f"пароль: от {MIN_PASSWORD} до {MAX_PASSWORD} символов")
    return password


def _display_name(name: str | None, fallback: str) -> str:
    name = (name or "").strip() or fallback
    if len(name) > 64:
        raise InvalidInput("имя: не длиннее 64 символов")
    return name


def _insert_session(conn, user_id: str, ttl_seconds: int) -> Session:
    rnd = secrets.token_urlsafe(32)
    row = conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) "
        "SELECT %s::text, %s::text, now() + make_interval(secs => %s::double precision) "
        "WHERE EXISTS (SELECT 1 FROM users WHERE user_id = %s) "
        "RETURNING expires_at",
        (hashlib.sha256(rnd.encode()).hexdigest(), user_id, ttl_seconds, user_id),
    ).fetchone()
    if row is None:
        raise UnknownUser(user_id)
    conn.execute("DELETE FROM sessions WHERE user_id = %s AND expires_at < now()", (user_id,))
    return Session(user_id=user_id, token=f"{user_id}.{rnd}", expires_at=row["expires_at"])


def issue_session(user_id: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> Session:
    validate_login(user_id)
    with get_conn(user_id) as conn:
        return _insert_session(conn, user_id, ttl_seconds)


def verify_session(token: object) -> str | None:
    if not isinstance(token, str) or len(token) > 256:
        return None
    user_id, dot, rnd = token.rpartition(".")
    if not dot or not rnd or not LOGIN_RE.fullmatch(user_id):
        return None
    with get_conn(user_id) as conn:
        row = conn.execute(
            "SELECT user_id FROM sessions "
            "WHERE token_hash = %s AND user_id = %s AND expires_at > now()",
            (hashlib.sha256(rnd.encode()).hexdigest(), user_id),
        ).fetchone()
    return row["user_id"] if row else None


def logout(token: object) -> bool:
    if not isinstance(token, str):
        return False
    user_id, dot, rnd = token.rpartition(".")
    if not dot or not rnd or not LOGIN_RE.fullmatch(user_id):
        return False
    with get_conn(user_id) as conn:
        row = conn.execute(
            "DELETE FROM sessions WHERE token_hash = %s AND user_id = %s RETURNING 1 AS gone",
            (hashlib.sha256(rnd.encode()).hexdigest(), user_id),
        ).fetchone()
    return row is not None


def register(login: str, password: str, display_name: str | None = None) -> Session:
    validate_login(login)
    _validate_password(password)
    name = _display_name(display_name, login)
    pw_hash = hash_password(password)
    with get_conn(login) as conn:
        row = conn.execute(
            "INSERT INTO users (user_id, password_hash, display_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
            (login, pw_hash, name),
        ).fetchone()
        if row is None:
            raise LoginTaken(login)
        return _insert_session(conn, login, SESSION_TTL_SECONDS)


def login(login: str, password: str) -> Session:
    try:
        validate_login(login)
        _validate_password(password)
    except InvalidInput:
        raise InvalidCredentials() from None
    with get_conn(login) as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE user_id = %s", (login,)
        ).fetchone()
        if not verify_password(password, row["password_hash"] if row else None):
            raise InvalidCredentials()
        return _insert_session(conn, login, SESSION_TTL_SECONDS)


def ensure_profile(
    user_id: str, display_name: str | None = None, avatar_url: str | None = None
) -> bool:
    validate_login(user_id)
    name = _display_name(display_name, user_id)
    with get_conn(user_id) as conn:
        row = conn.execute(
            "INSERT INTO users (user_id, display_name, avatar_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO NOTHING RETURNING user_id",
            (user_id, name, avatar_url),
        ).fetchone()
    return row is not None
