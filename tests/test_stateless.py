"""The stateless-era probe: a refusal of 2026-07-28 is a skip, anything else is not.

Replies are the ones trunk 7a96f6086ec sends to `server/discover` without a
session, on both endpoints.
"""

from typing import cast, override

import pytest

import mcp_client as C
from functional import stateless as S
from functional.reporting import Reporter
from mcp_client import Endpoint, admin_endpoint
from tests.stubs import const
from tests.test_mcp_client import FakeResp

ADMIN: Endpoint = admin_endpoint(base_url="http://shop.example")
SUPPORTED = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]


def refusal(requested: str = "2026-07-28", supported: list[str] = SUPPORTED, code: int = -32022) -> object:
    return {
        "jsonrpc": "2.0",
        "error": {
            "code": code,
            "message": "Unsupported protocol version",
            "data": {"requested": requested, "supported": supported},
        },
    }


def test_a_refusal_of_the_stateless_revision_is_the_skip_naming_the_core_pr() -> None:
    verdict, reason = S.classify(400, refusal())

    assert verdict == "skip"
    assert "withoutModernEra()" in reason
    assert "#19969" in reason


def test_an_answer_fails_until_the_scenarios_exist() -> None:
    """The day core serves the revision, the skip must not quietly stay a skip."""
    verdict, reason = S.classify(200, {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}})

    assert (verdict, reason) == ("fail", S.NO_SCENARIOS)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(400, refusal(requested="2025-11-25"), id="refused-a-revision-the-probe-did-not-ask-for"),
        pytest.param(400, refusal(code=-32600), id="another-error-code"),
        pytest.param(401, "Unauthorized", id="not-json-rpc-at-all"),
    ],
)
def test_any_other_refusal_is_a_failure_not_a_skip(status: int, body: object) -> None:
    """Bad auth or a probe the server did not read as stateless must not pass as
    "not served yet"."""
    verdict, reason = S.classify(status, body)

    assert verdict == "fail"
    assert f"HTTP {status}" in reason


def test_missing_revisions_names_what_the_refusal_left_out() -> None:
    assert S.missing_revisions(refusal()) == []
    assert S.missing_revisions(refusal(supported=["2025-06-18", "2025-11-25"])) == ["2024-11-05", "2025-03-26"]


def records(rep: Reporter) -> list[tuple[str, str]]:
    return [(r["label"], r["status"]) for r in rep.records]


def test_verify_records_the_skip_and_the_handshake_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "mcp_discover", const((400, refusal())))
    rep = Reporter("http://shop.example", color=False)

    S.verify_stateless_era(rep, ADMIN)

    assert (rep.passed, rep.failed, rep.skipped) == (1, 0, 1)
    assert records(rep) == [
        ("stateless 2026-07-28 (admin)", "skipped"),
        ("handshake revisions offered (admin)", "pass"),
    ]


def test_verify_fails_when_a_tested_revision_is_no_longer_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "mcp_discover", const((400, refusal(supported=SUPPORTED[1:]))))
    rep = Reporter("http://shop.example", color=False)

    S.verify_stateless_era(rep, ADMIN)

    assert (rep.passed, rep.failed, rep.skipped) == (0, 1, 1)
    assert ("handshake revisions offered (admin)", "fail") in records(rep)


def test_verify_fails_on_an_answer_without_checking_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    answer: object = {"jsonrpc": "2.0", "id": 1, "result": {}}
    monkeypatch.setattr(S, "mcp_discover", const((200, answer)))
    rep = Reporter("http://shop.example", color=False)

    S.verify_stateless_era(rep, ADMIN)

    assert (rep.passed, rep.failed, rep.skipped) == (0, 1, 0)


def test_discover_sends_no_session_and_the_revision_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}

    def fake_post(_url: str, *, headers: dict[str, str], json: object, timeout: int) -> FakeResp:
        sent.update(headers=headers, json=json, timeout=timeout)
        return FakeResp(400, headers={"Content-Type": "application/json"}, body=refusal())

    monkeypatch.setattr(C.requests, "post", fake_post)

    status, body = C.mcp_discover(ADMIN)

    assert (status, body) == (400, refusal())
    headers = cast(dict[str, str], sent["headers"])
    assert headers[C.PROTOCOL_HEADER] == C.STATELESS_REVISION
    assert "Mcp-Session-Id" not in headers
    assert sent["json"] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": C.STATELESS_REVISION}},
    }


def test_discover_hands_back_a_body_that_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class NotJson(FakeResp):
        @override
        def json(self) -> object:
            raise ValueError("not json")

    monkeypatch.setattr(C.requests, "post", const(NotJson(502, text="Bad Gateway")))

    assert C.mcp_discover(ADMIN) == (502, "Bad Gateway")
