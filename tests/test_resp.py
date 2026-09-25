"""The RESP subset functional/sessions.py talks to Redis with."""

from __future__ import annotations

import io
import socket

import pytest

from functional import resp as R
from functional.resp import Redis, RedisError
from tests.stubs import const


def reply(raw: bytes) -> R.Reply:
    return R.decode(io.BytesIO(raw))


def test_commands_are_arrays_of_bulk_strings() -> None:
    assert R.encode("GET", "kéy") == b"*2\r\n$3\r\nGET\r\n$4\r\nk\xc3\xa9y\r\n"


def test_every_reply_type_the_checks_read() -> None:
    assert reply(b"+OK\r\n") == "OK"
    assert reply(b":3600\r\n") == 3600
    assert reply(b"$-1\r\n") is None
    assert reply(b"*-1\r\n") is None
    assert reply(b"*2\r\n$1\r\n0\r\n*1\r\n$3\r\nkey\r\n") == ["0", ["key"]]


def test_a_bulk_string_round_trips_byte_for_byte() -> None:
    """A serialized PHP value is not UTF-8; the registry check searches inside one."""
    assert reply(b"$4\r\n\xff\x00ab\r\n") == "\xff\x00ab"


def test_an_error_reply_raises() -> None:
    with pytest.raises(RedisError, match="WRONGTYPE"):
        _ = reply(b"-WRONGTYPE Operation against a key\r\n")


def test_a_closed_connection_raises_rather_than_returning_nothing() -> None:
    with pytest.raises(RedisError, match="closed"):
        _ = reply(b"")


def test_an_unknown_reply_type_raises() -> None:
    with pytest.raises(RedisError, match="unexpected"):
        _ = reply(b"%1\r\n")


class FakeSocket:
    def __init__(self, replies: bytes) -> None:
        self.sent: list[bytes] = []
        self.replies: bytes = replies
        self.closed: bool = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def makefile(self, _mode: str) -> io.BytesIO:
        return io.BytesIO(self.replies)

    def close(self) -> None:
        self.closed = True


def connect(
    monkeypatch: pytest.MonkeyPatch, replies: bytes, url: str = "redis://lane:6379"
) -> tuple[Redis, FakeSocket]:
    sock = FakeSocket(replies)
    monkeypatch.setattr(socket, "create_connection", const(sock))
    return Redis(url), sock


def test_the_url_selects_its_database_and_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    _, sock = connect(monkeypatch, b"+OK\r\n+OK\r\n", "redis://:secret@lane:6379/5")

    assert sock.sent == [R.encode("AUTH", "secret"), R.encode("SELECT", "5")]


def test_keys_scans_until_the_cursor_returns_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, sock = connect(
        monkeypatch,
        b"*2\r\n$2\r\n17\r\n*1\r\n$1\r\na\r\n" + b"*2\r\n$1\r\n0\r\n*1\r\n$1\r\nb\r\n",
    )

    assert redis.keys("*") == ["a", "b"]
    assert sock.sent[1] == R.encode("SCAN", "17", "MATCH", "*", "COUNT", "1000")


def test_a_malformed_scan_reply_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, _ = connect(monkeypatch, b":1\r\n")

    with pytest.raises(RedisError, match="SCAN"):
        _ = redis.keys("*")


def test_ttl_get_delete_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, sock = connect(monkeypatch, b":3600\r\n$2\r\nhi\r\n:1\r\n+OK\r\n$-1\r\n+OK\r\n")

    assert redis.ttl("k") == 3600
    assert redis.get("k") == "hi"
    assert redis.delete("k") == 1
    # A reply of the wrong type reads as "absent", not as a crash.
    assert redis.ttl("k") == -2
    assert redis.get("k") is None
    assert redis.delete("k") == 0
    redis.close()
    assert sock.closed


def test_only_redis_urls_are_accepted() -> None:
    with pytest.raises(RedisError, match="redis://"):
        _ = Redis("http://lane:6379")


def test_an_unreachable_server_is_a_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> socket.socket:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(socket, "create_connection", refuse)

    with pytest.raises(RedisError, match="cannot connect"):
        _ = Redis("redis://localhost:0")
