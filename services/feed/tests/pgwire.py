from __future__ import annotations

import json
import re
import socket
import struct
import uuid
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse


class PgError(Exception):
    pass


def _quote(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "ARRAY[" + ",".join(_quote(x) for x in v) + "]::text[]" if v else "'{}'::text[]"
    if isinstance(v, dict):
        v = json.dumps(v)
    if isinstance(v, datetime):
        v = v.isoformat()
    s = str(v)
    if "\x00" in s:
        raise ValueError("NUL в параметре")
    return "'" + s.replace("'", "''") + "'"


def substitute(sql: str, params) -> str:
    values = list(params or ())
    it = iter(values)

    def repl(m):
        if m.group(0) == "%%":
            return "%"
        try:
            return _quote(next(it))
        except StopIteration:
            raise ValueError("параметров меньше, чем %s") from None

    out = re.sub(r"%%|%s", repl, sql)
    if next(it, None) is not None:
        raise ValueError("параметров больше, чем %s")
    return out


def _parse_array(text: str) -> list:
    body, items, cur, quoted, i = text[1:-1], [], "", False, 0
    if not body:
        return []
    while i < len(body):
        ch = body[i]
        if quoted:
            if ch == "\\":
                i += 1
                cur += body[i]
            elif ch == '"':
                quoted = False
            else:
                cur += ch
        elif ch == '"':
            quoted = True
        elif ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    items.append(cur)
    return items


def _decode(oid: int, raw: bytes | None):
    if raw is None:
        return None
    v = raw.decode("utf-8")
    if oid == 16:
        return v == "t"
    if oid in (20, 21, 23, 26):
        return int(v)
    if oid in (700, 701, 1700):
        return float(v)
    if oid in (1114, 1184):
        return datetime.fromisoformat(v)
    if oid == 2950:
        return uuid.UUID(v)
    if oid in (114, 3802):
        return json.loads(v)
    if oid in (1009, 1015):
        return _parse_array(v)
    return v


class Cursor:
    def __init__(self, rows: list[dict], rowcount: int):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class Connection:
    def __init__(self, host: str, port: int, user: str, dbname: str):
        self._sock = socket.create_connection((host, port), timeout=10)
        params = f"user\0{user}\0database\0{dbname}\0\0".encode()
        self._sock.sendall(struct.pack("!II", len(params) + 8, 196608) + params)
        while True:
            kind, body = self._read()
            if kind == b"R" and struct.unpack("!I", body[:4])[0] != 0:
                raise PgError("поддерживается только trust-аутентификация")
            if kind == b"E":
                raise PgError(self._error(body))
            if kind == b"Z":
                return

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise PgError("соединение закрыто сервером")
            buf += chunk
        return buf

    def _read(self) -> tuple[bytes, bytes]:
        kind = self._recv(1)
        (length,) = struct.unpack("!I", self._recv(4))
        return kind, self._recv(length - 4)

    @staticmethod
    def _error(body: bytes) -> str:
        parts = {}
        for field in body.split(b"\0"):
            if field:
                parts[field[:1]] = field[1:].decode("utf-8", "replace")
        return f"{parts.get(b'S', 'ERROR')} {parts.get(b'C', '')}: {parts.get(b'M', '')}"

    def execute(self, sql: str, params=None) -> Cursor:
        payload = substitute(sql, params).encode("utf-8") + b"\0"
        self._sock.sendall(b"Q" + struct.pack("!I", len(payload) + 4) + payload)
        cols: list[tuple[str, int]] = []
        raw_rows: list[list] = []
        tag, error = "", None
        while True:
            kind, body = self._read()
            if kind == b"T":
                (n,) = struct.unpack("!H", body[:2])
                pos, cols = 2, []
                for _ in range(n):
                    end = body.index(b"\0", pos)
                    name = body[pos:end].decode()
                    pos = end + 1
                    _, _, type_oid, _, _, _ = struct.unpack("!IhIhih", body[pos : pos + 18])
                    pos += 18
                    cols.append((name, type_oid))
            elif kind == b"D":
                (n,) = struct.unpack("!H", body[:2])
                pos, row = 2, []
                for _ in range(n):
                    (ln,) = struct.unpack("!i", body[pos : pos + 4])
                    pos += 4
                    if ln == -1:
                        row.append(None)
                    else:
                        row.append(body[pos : pos + ln])
                        pos += ln
                raw_rows.append(row)
            elif kind == b"C":
                tag = body.rstrip(b"\0").decode()
            elif kind == b"E":
                error = self._error(body)
            elif kind == b"Z":
                break
        if error:
            raise PgError(error)
        rows = [
            {name: _decode(oid, raw) for (name, oid), raw in zip(cols, row)} for row in raw_rows
        ]
        last = tag.split(" ")[-1]
        return Cursor(rows, int(last) if last.isdigit() else -1)

    def close(self) -> None:
        try:
            self._sock.sendall(b"X" + struct.pack("!I", 4))
        finally:
            self._sock.close()


@contextmanager
def connection(dsn: str):
    u = urlparse(dsn)
    conn = Connection(u.hostname or "127.0.0.1", u.port or 5432, u.username or "postgres",
                      u.path.lstrip("/"))
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
