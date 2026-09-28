"""Does the MCP server keep its sessions where the lane configured them?

The production setup for more than one worker is a Redis-backed session store
per server (`mcp.servers.<name>.session: {store: cache, cache_pool: …}`). Its
failure mode is quiet: a recipe that is not picked up leaves the file store in
place, and on a single machine every endpoint keeps working. So nothing here
asks whether sessions *work* — the rest of the suite already proves that on any
store. Each check is one the file store would fail:

  written      a key `<pool>:mcp-<server>-<session id>` appears on `initialize`
  expiring     it carries the session TTL, not the pool's or none
  registry     the active-session registry (what app-driven tools/list_changed
               broadcasts fan out to) is in the shared pool too. It is not by
               default — shopware/shopware#19980 — and the lane overrides it the
               way the docs describe, so this checks the documented workaround
  separate     the id is refused on the other endpoint. The two servers share one
               pool under different prefixes; a shared prefix would make a
               session minted on one valid on the other
  closed       `DELETE` removes the key
  read back    deleting the key behind the server's back makes it refuse the
               session. Without this, "a key appeared" only proves the server
               writes to Redis, not that it reads from there rather than a
               local fallback

Needs Redis access (`--redis-url`), because the property under test is where
state lives. Without it every check is skipped with that reason — a lane on the
file store has nothing to measure here.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

import requests

from functional.reporting import Reporter
from functional.resp import Redis, RedisError
from mcp_client import (
    ADMIN,
    BASE,
    STORE,
    SW_ACCESS_KEY,
    SW_BASE_URL,
    SW_SC_ACCESS_KEY,
    SW_SECRET_ACCESS_KEY,
    Endpoint,
    mcp_call,
    mcp_close,
    mcp_init,
)

# What the SDK answers for an id it has no session for — measured on mcp/sdk
# 0.8.1, HTTP 404 with JSON-RPC -32600. Asserted on, so an unrelated 404 (a
# route that moved) does not pass as a refusal.
SESSION_REFUSED = "Session not found or has expired."
PROBE = "shopware-toolsets-list"


@dataclass(frozen=True)
class Server:
    """One MCP server as the bundle names it. `name` is also its key prefix."""

    name: str
    endpoint: Endpoint
    registry_key: str


def servers() -> tuple[Server, Server]:
    return (
        Server("admin", ADMIN, "shopware.mcp.active_session_ids"),
        Server("store_api", STORE, "shopware.mcp.store_api.active_session_ids"),
    )


class KeyStore(Protocol):
    def keys(self, pattern: str) -> list[str]: ...
    def ttl(self, key: str) -> int: ...
    def get(self, key: str) -> str | None: ...
    def delete(self, key: str) -> int: ...


def find_key(store: KeyStore, name: str) -> str | None:
    """The key for `name` under whatever namespace the pool adds (`<ns>:name`)."""
    matches = [k for k in store.keys(f"*{name}") if k == name or k.endswith(f":{name}")]
    return matches[0] if len(matches) == 1 else None


def probe(session_id: str, endpoint: Endpoint) -> tuple[int, str]:
    """(HTTP status, body) of one call on this session; (200, "") if accepted."""
    try:
        _ = mcp_call(session_id, PROBE, {}, endpoint=endpoint)
    except requests.HTTPError as exc:
        reply = exc.response
        return (reply.status_code, reply.text) if reply is not None else (0, str(exc))
    return 200, ""


def refused(status: int, body: str) -> bool:
    return status == 404 and SESSION_REFUSED in body


def verify_server(rep: Reporter, store: KeyStore, server: Server, other: Server | None, ttl: int) -> None:
    rep.section(f"Session store: {server.name} ({server.endpoint.path})")
    session, _ = mcp_init(endpoint=server.endpoint)
    key = find_key(store, f"mcp-{server.name}-{session}")
    if key is None:
        # Everything below reads or removes this key, so there is nothing left
        # to check — and the likeliest cause is the one worth naming.
        rep.check_fail(
            f"{server.name}: session written to Redis",
            f"no key mcp-{server.name}-{session}: the server is not using the Redis store "
            f"(`debug:container mcp.server.{server.name}.session.store` still FileSessionStore?)",
        )
        return
    rep.check_pass(f"{server.name}: session written to Redis ({key})")

    remaining = store.ttl(key)
    if 0 < remaining <= ttl:
        rep.check_pass(f"{server.name}: session key expires with the session TTL ({remaining}s)")
    else:
        rep.check_fail(f"{server.name}: session key TTL", f"TTL {remaining}, expected 1..{ttl}")

    registry = find_key(store, server.registry_key)
    if registry is not None and session in (store.get(registry) or ""):
        rep.check_pass(f"{server.name}: active-session registry is in the shared pool")
    else:
        rep.check_fail(
            f"{server.name}: active-session registry",
            f"session not listed under {server.registry_key} in Redis — the registry is still local, so "
            "app-driven tools/list_changed broadcasts reach only this server's sessions (shopware/shopware#19980)",
        )

    status, body = probe(session, server.endpoint)
    if status != 200:
        rep.check_fail(f"{server.name}: session usable", f"HTTP {status}: {body[:160]}")
        return
    rep.check_pass(f"{server.name}: session usable")

    if other is None:
        rep.skip(f"{server.name}: refused on the other endpoint — no SW_SC_ACCESS_KEY, so no Store session to compare")
    else:
        status, body = probe(session, other.endpoint)
        if refused(status, body):
            rep.check_pass(f"{server.name}: session refused on {other.endpoint.path}")
        else:
            rep.check_fail(
                f"{server.name}: session refused on {other.endpoint.path}",
                f"HTTP {status}: {body[:160] or 'accepted'} — both servers read one session namespace",
            )

    closed, _ = mcp_init(endpoint=server.endpoint)
    closed_key = find_key(store, f"mcp-{server.name}-{closed}")
    status = mcp_close(closed, endpoint=server.endpoint)
    if closed_key is not None and 200 <= status < 300 and find_key(store, f"mcp-{server.name}-{closed}") is None:
        rep.check_pass(f"{server.name}: DELETE removes the session from Redis")
    else:
        rep.check_fail(
            f"{server.name}: DELETE removes the session",
            f"HTTP {status}, key before: {closed_key}, key after: {find_key(store, f'mcp-{server.name}-{closed}')}",
        )

    _ = store.delete(key)
    status, body = probe(session, server.endpoint)
    if refused(status, body):
        rep.check_pass(f"{server.name}: session refused once its Redis key is gone")
    else:
        rep.check_fail(
            f"{server.name}: session refused once its Redis key is gone",
            f"HTTP {status}: {body[:160] or 'accepted'} — the server still has the session somewhere else",
        )


def run(rep: Reporter, store: KeyStore, ttl: int, with_store_api: bool = True) -> None:
    """Both servers, each checked against the other. Without a sales-channel key
    there is no Store session to open, so that half is skipped by name rather
    than failing a lane that runs without the Store suite."""
    admin, store_api = servers()
    pairs: tuple[tuple[Server, Server | None], ...] = ((admin, store_api), (store_api, admin))
    if not with_store_api:
        rep.skip("store_api: session store — no SW_SC_ACCESS_KEY, the lane runs without the Store suite")
        pairs = ((admin, None),)
    for server, other in pairs:
        try:
            verify_server(rep, store, server, other, ttl)
        except (RuntimeError, requests.exceptions.RequestException) as exc:
            rep.check_fail(f"{server.name}: session store", f"aborted: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Where does the MCP server keep its sessions?")
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("MCP_EVALS_REDIS_URL", ""),
        help="The pool the lane's session stores use, e.g. redis://localhost:6379/5. Default: $MCP_EVALS_REDIS_URL.",
    )
    parser.add_argument("--session-ttl", type=int, default=3600, help="The configured session.ttl, in seconds.")
    args = parser.parse_args()
    redis_url = cast(str, args.redis_url)

    for name, value in (
        ("SW_BASE_URL", SW_BASE_URL),
        ("SW_ACCESS_KEY", SW_ACCESS_KEY),
        ("SW_SECRET_ACCESS_KEY", SW_SECRET_ACCESS_KEY),
    ):
        if not value:
            sys.exit(f"ERROR: {name} is required")

    rep = Reporter(SW_BASE_URL)
    rep.banner("Shopware MCP session store")
    if not redis_url:
        rep.skip("session store checks need --redis-url (or MCP_EVALS_REDIS_URL): nothing to measure on the file store")
    else:
        try:
            redis = Redis(redis_url)
        except RedisError as exc:
            rep.check_fail("redis", str(exc))
        else:
            try:
                run(rep, redis, cast(int, args.session_ttl), with_store_api=bool(SW_SC_ACCESS_KEY))
            finally:
                redis.close()

    rep.summary()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rep.write_report(BASE / "results" / f"functional-sessions-{timestamp}.json")
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
