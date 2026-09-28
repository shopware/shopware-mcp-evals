"""The session-store checks, against a fake lane that can be each misconfiguration.

The point of functional/sessions.py is that every check fails on a lane that is
not really on Redis, so the tests are mostly that: one fake per way of being
wrong, each asserting the check that names it — and that a correct lane passes
all of them.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
import requests

from eval.result_schema import JsonObject, McpResponse
from functional import sessions as S
from functional.reporting import Reporter
from functional.resp import RedisError
from mcp_client import Endpoint
from tests.stubs import const, raiser

NS = "ns1"


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def keys(self, pattern: str) -> list[str]:
        assert pattern.startswith("*")
        return [k for k in self.values if k.endswith(pattern[1:])]

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -2)

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return 1 if self.values.pop(key, None) is not None else 0


def server_of(endpoint: Endpoint) -> str:
    return "admin" if endpoint.path == "/api/_mcp" else "store_api"


def refusal(body: str = f'{{"error":{{"code":-32600,"message":"{S.SESSION_REFUSED}"}}}}') -> requests.HTTPError:
    reply = requests.Response()
    reply.status_code = 404
    reply._content = body.encode()
    return requests.HTTPError(response=reply)


class FakeLane:
    """What the server does with a session, for one way of configuring it.

    redis           sessions live in the store under mcp-<server>-<id>
    ttl             the TTL each session key gets (-1: none)
    shared_registry the active-session registry is in the store too
    one_namespace   both servers read the same sessions (a shared prefix)
    local_copy      the server keeps a copy it answers from after Redis loses it
    """

    def __init__(
        self,
        store: FakeRedis,
        *,
        redis: bool = True,
        ttl: int = 3600,
        shared_registry: bool = True,
        one_namespace: bool = False,
        local_copy: bool = False,
    ) -> None:
        self.store: FakeRedis = store
        self.redis: bool = redis
        self.ttl: int = ttl
        self.shared_registry: bool = shared_registry
        self.one_namespace: bool = one_namespace
        self.local_copy: bool = local_copy
        self.local: set[tuple[str, str]] = set()
        self.ids: itertools.count[int] = itertools.count(1)

    def key(self, server: str, session: str) -> str:
        return f"{NS}:mcp-{server}-{session}"

    def init(self, endpoint: Endpoint) -> tuple[str, str]:
        server = server_of(endpoint)
        session = f"sid-{next(self.ids)}"
        self.local.add((server, session))
        if self.redis:
            self.store.values[self.key(server, session)] = "{}"
            self.store.ttls[self.key(server, session)] = self.ttl
        if self.shared_registry:
            registry = f"{NS}:" + (
                "shopware.mcp.active_session_ids" if server == "admin" else "shopware.mcp.store_api.active_session_ids"
            )
            self.store.values[registry] = self.store.values.get(registry, "") + f's:{len(session)}:"{session}";'
        return session, ""

    def known(self, server: str, session: str) -> bool:
        servers = ("admin", "store_api") if self.one_namespace else (server,)
        for candidate in servers:
            if self.redis and self.key(candidate, session) in self.store.values:
                return True
            if (not self.redis or self.local_copy) and (candidate, session) in self.local:
                return True
        return False

    def call(self, session: str, _tool: str, _args: JsonObject, endpoint: Endpoint) -> McpResponse:
        if not self.known(server_of(endpoint), session):
            raise refusal()
        return {"jsonrpc": "2.0", "id": 99, "result": {}}

    def close(self, session: str, endpoint: Endpoint) -> int:
        server = server_of(endpoint)
        _ = self.store.delete(self.key(server, session))
        self.local.discard((server, session))
        return 204


def install(monkeypatch: pytest.MonkeyPatch, lane: FakeLane) -> None:
    monkeypatch.setattr(S, "mcp_init", lane.init)
    monkeypatch.setattr(S, "mcp_call", lane.call)
    monkeypatch.setattr(S, "mcp_close", lane.close)


def run(monkeypatch: pytest.MonkeyPatch, **config: bool | int) -> tuple[Reporter, FakeRedis]:
    store = FakeRedis()
    install(monkeypatch, FakeLane(store, **config))  # pyright: ignore[reportArgumentType]
    rep = Reporter("http://lane", color=False)
    S.run(rep, store, ttl=3600)
    return rep, store


def failed(rep: Reporter) -> list[str]:
    return [r.get("label", "") for r in rep.records if r.get("status") == "fail"]


# ---------------------------------------------------------------------------
# One lane per way of being wrong
# ---------------------------------------------------------------------------
def test_a_lane_really_on_redis_passes_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch)

    assert failed(rep) == []
    assert rep.passed == 14


def test_the_file_store_fails_the_first_check_and_names_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing after it can run — every later check reads or deletes the key."""
    rep, _ = run(monkeypatch, redis=False, shared_registry=False)

    assert failed(rep) == ["admin: session written to Redis", "store_api: session written to Redis"]
    assert rep.passed == 0
    assert "FileSessionStore" in str(rep.records[0].get("error"))


def test_a_session_without_its_ttl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, ttl=-1)

    assert failed(rep) == ["admin: session key TTL", "store_api: session key TTL"]


def test_a_local_registry_fails_and_points_at_the_core_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented recipe without the registry override: sessions shared,
    broadcasts not."""
    rep, _ = run(monkeypatch, shared_registry=False)

    assert failed(rep) == ["admin: active-session registry", "store_api: active-session registry"]
    assert "#19980" in str(rep.records[2].get("error"))


def test_one_session_namespace_for_both_servers_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    rep, _ = run(monkeypatch, one_namespace=True)

    assert failed(rep) == ["admin: session refused on /store-api/_mcp", "store_api: session refused on /api/_mcp"]


def test_a_server_that_answers_from_a_local_copy_fails_the_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing to Redis is not reading from it: this lane creates every key and
    would pass everything else."""
    rep, _ = run(monkeypatch, local_copy=True)

    assert failed(rep) == [
        "admin: session refused once its Redis key is gone",
        "store_api: session refused once its Redis key is gone",
    ]


def test_a_delete_that_leaves_the_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeRedis()
    lane = FakeLane(store)
    install(monkeypatch, lane)
    monkeypatch.setattr(S, "mcp_close", const(204))
    rep = Reporter("http://lane", color=False)

    S.run(rep, store, ttl=3600)

    assert failed(rep) == ["admin: DELETE removes the session", "store_api: DELETE removes the session"]


def test_an_unusable_session_stops_that_server(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeRedis()
    lane = FakeLane(store)
    install(monkeypatch, lane)
    monkeypatch.setattr(S, "mcp_call", raiser(refusal("upstream error")))
    rep = Reporter("http://lane", color=False)

    S.run(rep, store, ttl=3600)

    assert failed(rep) == ["admin: session usable", "store_api: session usable"]


def test_a_transport_failure_is_recorded_per_server_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "mcp_init", raiser(requests.ConnectionError("refused")))
    rep = Reporter("http://lane", color=False)

    S.run(rep, FakeRedis(), ttl=3600)

    assert failed(rep) == ["admin: session store", "store_api: session store"]


def test_a_redis_failure_mid_run_is_recorded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeRedis()
    install(monkeypatch, FakeLane(store))
    monkeypatch.setattr(store, "keys", raiser(RedisError("connection closed mid-reply")))
    rep = Reporter("http://lane", color=False)

    S.run(rep, store, ttl=3600)

    assert failed(rep) == ["admin: session store", "store_api: session store"]


# ---------------------------------------------------------------------------
# The two helpers the verdicts hinge on
# ---------------------------------------------------------------------------
def test_only_the_sdk_refusal_counts_as_refused() -> None:
    """A 404 from a route that moved is not a refused session."""
    assert S.refused(404, S.SESSION_REFUSED)
    assert not S.refused(404, "<html>Not Found</html>")
    assert not S.refused(400, S.SESSION_REFUSED)


def test_probe_reports_a_transport_error_without_a_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "mcp_call", raiser(requests.HTTPError("no reply")))

    assert S.probe("sid", S.servers()[0].endpoint) == (0, "no reply")


def test_find_key_matches_under_the_pool_namespace_only() -> None:
    store = FakeRedis()
    store.values = {
        "ns:shopware.mcp.active_session_ids": "",
        "ns:shopware.mcp.store_api.active_session_ids": "",
        "ns:xshopware.mcp.active_session_ids": "",
    }

    assert S.find_key(store, "shopware.mcp.active_session_ids") == "ns:shopware.mcp.active_session_ids"
    assert S.find_key(store, "shopware.mcp.store_api.active_session_ids") is not None
    assert S.find_key(store, "absent") is None


def test_find_key_refuses_to_pick_between_two_matches() -> None:
    store = FakeRedis()
    store.values = {"a:mcp-admin-1": "", "b:mcp-admin-1": ""}

    assert S.find_key(store, "mcp-admin-1") is None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("SW_BASE_URL", "SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "SW_SC_ACCESS_KEY"):
        monkeypatch.setattr(S, name, "set")
    monkeypatch.setattr(S, "BASE", tmp_path)
    monkeypatch.delenv("MCP_EVALS_REDIS_URL", raising=False)


def main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["functional.sessions", *argv])
    return S.main()


@pytest.mark.usefixtures("credentials")
def test_without_a_redis_url_it_skips_with_the_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert main(monkeypatch) == 0
    assert list((tmp_path / "results").glob("functional-sessions-*.json"))


@pytest.mark.usefixtures("credentials")
def test_an_unreachable_redis_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "Redis", raiser(RedisError("cannot connect")))

    assert main(monkeypatch, "--redis-url", "redis://localhost:0") == 1


@pytest.mark.usefixtures("credentials")
def test_main_runs_the_checks_and_closes_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeRedis()
    closed: list[bool] = []

    class Connected(FakeRedis):
        def __init__(self, _url: str) -> None:
            super().__init__()
            self.values: dict[str, str] = store.values
            self.ttls: dict[str, int] = store.ttls

        def close(self) -> None:
            closed.append(True)

    install(monkeypatch, FakeLane(store))
    monkeypatch.setattr(S, "Redis", Connected)

    assert main(monkeypatch, "--redis-url", "redis://lane:6379/5") == 0
    assert closed == [True]


@pytest.mark.usefixtures("credentials")
def test_the_admin_pair_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "SW_SECRET_ACCESS_KEY", "")

    with pytest.raises(SystemExit, match="SW_SECRET_ACCESS_KEY is required"):
        _ = main(monkeypatch)


def test_without_a_sales_channel_key_only_admin_is_checked_and_the_rest_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane without the Store suite has no Store session to open. That is a
    skip with the reason, not a failure — and not a silent pass either."""
    store = FakeRedis()
    install(monkeypatch, FakeLane(store))
    rep = Reporter("http://lane", color=False)

    S.run(rep, store, ttl=3600, with_store_api=False)

    assert failed(rep) == []
    assert rep.passed == 6
    assert rep.skipped == 2
