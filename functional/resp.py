"""The four Redis commands the session-store checks need, over a plain socket.

A client library would be one more runtime dependency, with its own stub
surface for basedpyright, to send SCAN, TTL, GET and DEL. RESP is small enough
that writing it out is shorter than justifying the dependency.

Only what the checks read is decoded: bulk strings, integers, arrays, nil and
errors. Bulk strings are decoded as latin-1 so a serialized PHP value round-trips
byte for byte — the registry check looks for a session id inside one.
"""

from __future__ import annotations

import socket
from typing import Protocol
from urllib.parse import urlparse

type Reply = str | int | None | list[Reply]


class RedisError(RuntimeError):
    """The server answered `-ERR ...`, or the connection could not be used."""


class LineReader(Protocol):
    def readline(self) -> bytes: ...
    def read(self, size: int, /) -> bytes: ...


def encode(*args: str) -> bytes:
    out = [f"*{len(args)}\r\n".encode()]
    for arg in args:
        raw = arg.encode()
        out.append(b"$%d\r\n%s\r\n" % (len(raw), raw))
    return b"".join(out)


def decode(stream: LineReader) -> Reply:
    line = stream.readline()
    if not line.endswith(b"\r\n"):
        raise RedisError("connection closed mid-reply")
    kind, body = line[:1], line[1:-2].decode("latin-1")
    if kind == b"+":
        return body
    if kind == b"-":
        raise RedisError(body)
    if kind == b":":
        return int(body)
    if kind == b"$":
        size = int(body)
        if size < 0:
            return None
        return stream.read(size + 2)[:-2].decode("latin-1")
    if kind == b"*":
        size = int(body)
        return None if size < 0 else [decode(stream) for _ in range(size)]
    raise RedisError(f"unexpected reply type {kind!r}")


class Redis:
    """One connection, selected onto the database the URL names."""

    def __init__(self, url: str, timeout: float = 5.0):
        parsed = urlparse(url)
        if parsed.scheme != "redis":
            raise RedisError(f"not a redis:// URL: {url}")
        try:
            self._sock: socket.socket = socket.create_connection(
                (parsed.hostname or "localhost", parsed.port or 6379), timeout=timeout
            )
        except OSError as exc:
            raise RedisError(f"cannot connect to {url}: {exc}") from exc
        self._stream: LineReader = self._sock.makefile("rb")
        if parsed.password:
            _ = self.command("AUTH", parsed.password)
        if db := parsed.path.lstrip("/"):
            _ = self.command("SELECT", db)

    def command(self, *args: str) -> Reply:
        self._sock.sendall(encode(*args))
        return decode(self._stream)

    def keys(self, pattern: str) -> list[str]:
        """SCAN to completion — KEYS would block a shared server."""
        found: list[str] = []
        cursor = "0"
        while True:
            reply = self.command("SCAN", cursor, "MATCH", pattern, "COUNT", "1000")
            if not isinstance(reply, list) or len(reply) != 2 or not isinstance(reply[1], list):
                raise RedisError(f"unexpected SCAN reply: {reply!r}")
            cursor = str(reply[0])
            found.extend(str(key) for key in reply[1])
            if cursor == "0":
                return found

    def ttl(self, key: str) -> int:
        reply = self.command("TTL", key)
        return reply if isinstance(reply, int) else -2

    def get(self, key: str) -> str | None:
        reply = self.command("GET", key)
        return reply if isinstance(reply, str) else None

    def delete(self, key: str) -> int:
        reply = self.command("DEL", key)
        return reply if isinstance(reply, int) else 0

    def close(self) -> None:
        self._sock.close()
