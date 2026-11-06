"""The session binding checks, with the server and the Admin API faked."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager

import pytest

from functional import session_binding as B
from functional.principals import Principal, ProvisioningFailed
from functional.reporting import Reporter
from functional.sessions import SESSION_REFUSED
from mcp_client import Endpoint, admin_endpoint
from tests.stubs import const, raiser
from tests.test_mcp_client import FakeResp

ADMIN: Endpoint = admin_endpoint(access_key="SWUA", secret_access_key="secret", base_url="http://shop.example")
OTHER = Principal("other", "SWIAOTHER", "other-secret", "all")
REFUSAL = (404, f'{{"jsonrpc":"2.0","error":{{"code":-32600,"message":"{SESSION_REFUSED}"}}}}')


class Lane:
    """Who may use a session, decided the way the server under test decides it."""

    def __init__(self, bound: bool = True, delete_status: int = 404, keeps_on_new_token: bool = True) -> None:
        self.bound: bool = bound
        self.delete_status: int = delete_status
        self.keeps_on_new_token: bool = keeps_on_new_token
        self.tokens: Iterator[str] = iter(["suite-token", "token-1", "token-2"])

    def probe(self, _session: str, endpoint: Endpoint) -> tuple[int, str]:
        auth = endpoint.auth_headers
        if auth.get("sw-access-key") == OTHER.access_key and self.bound:
            return REFUSAL
        if auth.get("Authorization") == "Bearer token-2" and not self.keeps_on_new_token:
            return REFUSAL
        return 200, ""

    def delete(self, _url: str, **_kwargs: object) -> FakeResp:
        return FakeResp(self.delete_status)


class FakeApi:
    def __init__(self, lane: Lane) -> None:
        self.token: str = next(lane.tokens)


def stub(monkeypatch: pytest.MonkeyPatch, lane: Lane) -> None:
    @contextmanager
    def another(_api: object) -> Generator[Principal]:
        yield OTHER

    def api(*_args: object) -> FakeApi:
        return FakeApi(lane)

    monkeypatch.setattr(B, "AdminApi", api)
    monkeypatch.setattr(B, "another_principal", another)
    monkeypatch.setattr(B, "mcp_init", const(("session", "")))
    monkeypatch.setattr(B, "probe", lane.probe)
    monkeypatch.setattr(B.requests, "delete", lane.delete)


def statuses(rep: Reporter) -> dict[str, str]:
    return {r["label"]: r["status"] for r in rep.records}


def run(lane: Lane, monkeypatch: pytest.MonkeyPatch) -> Reporter:
    stub(monkeypatch, lane)
    rep = Reporter("http://shop.example", color=False)
    B.verify_session_binding(rep, ADMIN, provision=True)
    return rep


def test_a_bound_server_passes_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(Lane(), monkeypatch)

    assert statuses(rep) == dict.fromkeys(B.LABELS, "pass")


def test_a_server_without_the_binding_is_the_skip_naming_the_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(Lane(bound=False), monkeypatch)

    assert statuses(rep) == {B.FOREIGN_CALL: "skipped"}
    assert "#21860" in rep.records[0].get("reason", "")


def test_a_foreign_delete_that_ends_the_session_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(Lane(delete_status=204), monkeypatch)

    assert statuses(rep)[B.FOREIGN_CLOSE] == "fail"


def test_a_session_lost_on_a_new_token_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(Lane(keeps_on_new_token=False), monkeypatch)

    assert statuses(rep)[B.NEW_TOKEN] == "fail"


def test_a_refusal_other_than_the_unknown_session_one_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    lane = Lane()
    stub(monkeypatch, lane)
    monkeypatch.setattr(B, "probe", const((403, "Forbidden")))
    rep = Reporter("http://shop.example", color=False)

    B.verify_session_binding(rep, ADMIN, provision=True)

    status = statuses(rep)
    assert status[B.FOREIGN_CALL] == "fail"
    assert status[B.OWNER_KEEPS] == "fail"
    assert status[B.NEW_TOKEN] == "fail"


def test_the_same_token_twice_proves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    lane = Lane()
    lane.tokens = iter(["suite-token", "same", "same"])

    rep = run(lane, monkeypatch)

    assert statuses(rep)[B.NEW_TOKEN] == "fail"


def test_without_provisioning_every_check_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, Lane())
    rep = Reporter("http://shop.example", color=False)

    B.verify_session_binding(rep, ADMIN, provision=False)

    assert statuses(rep) == dict.fromkeys(B.LABELS, "skipped")


def test_a_provisioning_failure_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, Lane())
    monkeypatch.setattr(B, "AdminApi", raiser(ProvisioningFailed("no token")))
    rep = Reporter("http://shop.example", color=False)

    B.verify_session_binding(rep, ADMIN, provision=True)

    assert statuses(rep) == {B.FOREIGN_CALL: "fail"}


def test_the_bearer_endpoint_keeps_the_revision_and_drops_the_key_headers() -> None:
    bearer = B.bearer_endpoint(ADMIN.with_protocol("2025-06-18"), "tok")

    assert bearer.auth_headers["Authorization"] == "Bearer tok"
    assert "sw-access-key" not in bearer.auth_headers
    assert bearer.protocol_version == "2025-06-18"
    assert bearer.url == ADMIN.url
