"""The stateless era: the probe, the 2026-07-28 transport and the ADR scenarios.

The refusals are the ones trunk 7a96f6086ec sends to `server/discover` on both
endpoints. The answers, tool results and listen streams are the ones the trunk
lane sent on 2026-10-27 with `shopware.mcp.protocol_era: auto`, trimmed.
"""

import base64
from collections.abc import Callable, Iterator
from typing import Self, cast, override

import pytest

import mcp_client as C
from eval.result_schema import JsonObject, McpResponse
from functional import stateless as S
from functional.principals import Principal
from functional.reporting import Reporter
from mcp_client import Endpoint, ListenFrame, StatelessReply, admin_endpoint, store_endpoint
from tests.stubs import const
from tests.test_mcp_client import FakeResp

ADMIN: Endpoint = admin_endpoint(access_key="SWUA", secret_access_key="secret", base_url="http://shop.example")
STORE: Endpoint = store_endpoint(access_key="SWSC", context_token="ctx", base_url="http://shop.example")
SUPPORTED = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]
SIGNED = "shopware://tool-result/01a17e0113f970888a715914cd789daa.1793118962.1A3siLgDlBJMyuH6xbjBZcVmlTYhbS2I"
HINT = (
    "shopware-toolset-enable is not available on protocol revision 2026-07-28, which has no MCP session to keep "
    'toolsets in. If your MCP client only calls advertised tools, reconnect with "?toolsets=dev-extensions".'
)


def refusal(requested: str = "2026-07-28", supported: list[str] = SUPPORTED, code: int = -32022) -> object:
    return {
        "jsonrpc": "2.0",
        "error": {
            "code": code,
            "message": "Unsupported protocol version",
            "data": {"requested": requested, "supported": supported},
        },
    }


def answer(listchanged: bool = True) -> object:
    tools: JsonObject = {"listChanged": True} if listchanged else {}
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": tools, "resources": {}}},
    }


def message(body: JsonObject) -> McpResponse:
    return cast(McpResponse, cast(object, body))


def tool_reply(*content: JsonObject, is_error: bool = False, session_id: str | None = None) -> StatelessReply:
    # Stamped like every result core's renderer builds, which is what marks the spec format.
    stamp: JsonObject = {
        "shopware/generatedAt": "2026-10-27T15:36:02+00:00",
        "shopware/expiresAt": "2026-10-27T16:36:02+00:00",
    }
    result: JsonObject = {"content": list(content), "_meta": stamp}
    if is_error:
        result["isError"] = True
    return StatelessReply(200, message({"jsonrpc": "2.0", "id": 1, "result": result}), session_id)


def text(value: str) -> JsonObject:
    return {"type": "text", "text": value}


# The two text blocks the lane sent next to the link: the note, then the metadata.
OFFLOADED: tuple[JsonObject, ...] = (
    {"type": "text", "text": "Response too large for inline delivery. Fetch it via resources/read."},
    {"type": "text", "text": '{"_meta":{"total":250,"page":1,"limit":500,"responseSize":346049}}'},
)


def link(uri: str = SIGNED) -> JsonObject:
    return {"type": "resource_link", "uri": uri, "name": "result"}


def records(rep: Reporter) -> list[tuple[str, str]]:
    return [(r["label"], r["status"]) for r in rep.records]


def reporter() -> Reporter:
    return Reporter("http://shop.example", color=False)


# --- the probe ---


def test_a_refusal_of_the_stateless_revision_is_the_skip_naming_the_switch() -> None:
    verdict, reason = S.classify(400, refusal())

    assert verdict == "skip"
    assert "protocol_era" in reason
    assert "#19969" in reason


def test_an_answer_naming_the_revision_is_served() -> None:
    assert S.classify(200, answer()) == ("serve", "")


def test_an_answer_without_the_revision_is_a_failure() -> None:
    verdict, reason = S.classify(200, {"jsonrpc": "2.0", "id": 1, "result": {"supportedVersions": ["2025-11-25"]}})

    assert verdict == "fail"
    assert "supportedVersions" in reason


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


def test_verify_records_the_skip_and_the_handshake_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "mcp_discover", const((400, refusal())))
    rep = reporter()

    S.verify_stateless_era(rep, ADMIN)

    assert records(rep) == [
        ("stateless 2026-07-28 (admin)", "skipped"),
        ("handshake revisions offered (admin)", "pass"),
    ]


def test_verify_fails_when_a_tested_revision_is_no_longer_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "mcp_discover", const((400, refusal(supported=SUPPORTED[1:]))))
    rep = reporter()

    S.verify_stateless_era(rep, ADMIN)

    assert (rep.passed, rep.failed, rep.skipped) == (0, 1, 1)
    assert ("handshake revisions offered (admin)", "fail") in records(rep)


def test_verify_stops_at_an_answer_it_cannot_read(monkeypatch: pytest.MonkeyPatch) -> None:
    unreadable: object = {"jsonrpc": "2.0", "id": 1, "result": {}}
    monkeypatch.setattr(S, "mcp_discover", const((200, unreadable)))
    rep = reporter()

    S.verify_stateless_era(rep, ADMIN)

    assert (rep.passed, rep.failed, rep.skipped) == (0, 1, 0)


# --- the transport ---


def test_a_request_carries_its_revision_capabilities_and_method() -> None:
    """mcp/sdk 0.9 refuses a request without the capabilities member (-32602) or
    without `Mcp-Method` (-32020); the first probe sent neither."""
    headers, body = C.stateless_request("server/discover", {})

    assert headers[C.PROTOCOL_HEADER] == C.STATELESS_REVISION
    assert headers[C.METHOD_HEADER] == "server/discover"
    assert C.NAME_HEADER not in headers
    assert "Mcp-Session-Id" not in headers
    meta = cast(JsonObject, cast(JsonObject, body["params"])["_meta"])
    assert meta[C.META_PROTOCOL_VERSION] == C.STATELESS_REVISION
    assert meta[C.META_CLIENT_CAPABILITIES] == {}


@pytest.mark.parametrize(
    ("method", "params", "name"),
    [
        ("tools/call", {"name": "shopware-entity-search", "arguments": {}}, "shopware-entity-search"),
        ("prompts/get", {"name": "shopware-context"}, "shopware-context"),
        ("resources/read", {"uri": SIGNED}, SIGNED),
    ],
)
def test_a_request_with_a_subject_repeats_it_in_mcp_name(method: str, params: JsonObject, name: str) -> None:
    headers, body = C.stateless_request(method, params)

    assert headers[C.NAME_HEADER] == name
    assert {k: v for k, v in cast(JsonObject, body["params"]).items() if k != "_meta"} == params


@pytest.mark.parametrize(
    ("value", "wrapped"),
    [
        pytest.param("plain name", False, id="printable-ascii"),
        pytest.param(" padded", True, id="leading-space"),
        pytest.param("grüße", True, id="non-ascii"),
        pytest.param("=?base64?eA==?=", True, id="looks-wrapped-already"),
    ],
)
def test_a_header_value_is_wrapped_only_when_it_has_to_be(value: str, wrapped: bool) -> None:
    out = C.header_value(value)

    if not wrapped:
        assert out == value
        return
    assert out.startswith("=?base64?") and out.endswith("?=")
    assert base64.b64decode(out[len("=?base64?") : -2]).decode() == value


def test_a_reply_keeps_status_message_and_a_minted_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, str]] = []

    def post(_url: str, *, headers: dict[str, str], json: object, timeout: int) -> FakeResp:
        del json, timeout
        sent.append(headers)
        return FakeResp(
            200,
            headers={"Content-Type": "application/json", "Mcp-Session-Id": "abc"},
            body={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
        )

    monkeypatch.setattr(C.requests, "post", post)

    reply = C.mcp_stateless("tools/list", None, ADMIN)

    assert (reply.status, reply.session_id, reply.result, reply.error) == (200, "abc", {"tools": []}, {})
    assert sent[0]["sw-access-key"] == "SWUA"


def test_a_throttled_request_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter(
        [
            FakeResp(429, headers={"Retry-After": "0"}, body={}),
            FakeResp(200, headers={"Content-Type": "application/json"}, body={"jsonrpc": "2.0", "id": 1, "result": {}}),
        ]
    )

    def post(*_args: object, **_kwargs: object) -> FakeResp:
        return next(replies)

    monkeypatch.setattr(C.requests, "post", post)
    monkeypatch.setattr(C.time, "sleep", const(None))

    assert C.mcp_stateless("tools/list", {}, ADMIN).status == 200


def test_a_reply_that_is_not_json_becomes_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class NotJson(FakeResp):
        @override
        def json(self) -> object:
            raise ValueError("not json")

    monkeypatch.setattr(C.requests, "post", const(NotJson(502, text="Bad   Gateway")))

    assert C.mcp_discover(ADMIN) == (502, {"error": {"message": "Bad Gateway"}})


def test_stateless_tools_follows_the_cursor_and_raises_on_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = iter(
        [
            StatelessReply(200, message({"result": {"tools": [{"name": "a"}], "nextCursor": "2"}}), None),
            StatelessReply(200, message({"result": {"tools": [{"name": "b"}]}}), None),
        ]
    )

    def page(*_args: object) -> StatelessReply:
        return next(pages)

    monkeypatch.setattr(C, "mcp_stateless", page)

    assert C.mcp_stateless_tools(ADMIN) == ["a", "b"]

    monkeypatch.setattr(C, "mcp_stateless", const(StatelessReply(400, message({"error": {"message": "no"}}), None)))
    with pytest.raises(RuntimeError, match="no"):
        C.mcp_stateless_tools(ADMIN)


def test_stateless_tools_gives_up_on_a_cursor_that_never_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    looping = StatelessReply(200, message({"result": {"tools": [], "nextCursor": "again"}}), None)
    monkeypatch.setattr(C, "mcp_stateless", const(looping))

    with pytest.raises(RuntimeError, match="50 pages"):
        C.mcp_stateless_tools(ADMIN)


class FakeStream:
    """A streamed reply: the lines of an SSE body, read one at a time."""

    def __init__(self, lines: list[str], content_type: str = "text/event-stream", body: object = None) -> None:
        self.lines: list[str] = lines
        self.headers: dict[str, str] = {"Content-Type": content_type}
        self.status_code: int = 200 if body is None else 400
        self.text: str = "" if body is None else "x"
        self._body: object = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[str]:
        del decode_unicode
        yield from self.lines

    def json(self) -> object:
        return self._body


ACK_LINE = 'data: {"jsonrpc":"2.0","method":"notifications/subscriptions/acknowledged","params":{}}'
CHANGE_LINE = 'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed","params":{}}'
CLOSE_LINE = 'data: {"jsonrpc":"2.0","id":1,"result":{"resultType":"complete"}}'


def test_listen_runs_the_change_after_the_ack_and_stops_when_told(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeStream(["event: message", ACK_LINE, "", "data: null", "data: {broken", CHANGE_LINE, CLOSE_LINE])
    monkeypatch.setattr(C.requests, "post", const(stream))
    acked: list[bool] = []

    frames = C.mcp_listen(
        ADMIN,
        {"toolsListChanged": True},
        on_ack=lambda: acked.append(True),
        until=lambda f: f.method == S.LIST_CHANGED,
    )

    assert [f.method for f in frames] == [C.ACKNOWLEDGED, S.LIST_CHANGED]
    assert acked == [True]


def test_listen_stops_reading_after_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 100.0, 100.0])
    monkeypatch.setattr(C.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(C.requests, "post", const(FakeStream([ACK_LINE, CLOSE_LINE])))

    assert C.mcp_listen(ADMIN, {}, timeout_s=1) == []


def test_listen_hands_back_a_refusal_that_was_not_streamed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        C.requests, "post", const(FakeStream([], content_type="application/json", body={"error": {"message": "no"}}))
    )

    (frame,) = C.mcp_listen(ADMIN, {})

    assert frame.message == {"status": 400, "error": {"message": "no"}}


# --- the scenarios ---


def test_a_hidden_session_tool_that_points_to_toolsets_passes() -> None:
    assert S.session_tool_problem(False, tool_reply(text(HINT), is_error=True)) is None


@pytest.mark.parametrize(
    ("advertised", "reply", "problem"),
    [
        pytest.param(True, tool_reply(text(HINT), is_error=True), "advertised", id="advertised"),
        pytest.param(False, tool_reply(text('{"enabled": true}')), "succeeded", id="silently-succeeds"),
        pytest.param(False, tool_reply(text("No session."), is_error=True), "does not point", id="no-hint"),
        pytest.param(
            False,
            StatelessReply(400, message({"error": {"code": -32602, "message": "bad"}}), None),
            "transport",
            id="transport-error",
        ),
    ],
)
def test_a_session_tool_fails_for_each_way_it_misleads(advertised: bool, reply: StatelessReply, problem: str) -> None:
    assert problem in (S.session_tool_problem(advertised, reply) or "")


def test_pinned_tools_compare_without_the_session_tools() -> None:
    handshake = ["shopware-tool-search", "shopware-toolset-enable", "shopware-toolsets-list", "a", "b"]

    assert S.pinned_problem(handshake, ["shopware-tool-search", "a", "b"], ["shopware-tool-search", "a", "b"]) is None
    problem = S.pinned_problem(handshake, ["shopware-tool-search", "a", "c"], ["shopware-tool-search", "c", "a"]) or ""
    assert "only on handshake: ['b']" in problem
    assert "only on stateless: ['c']" in problem
    assert "second identical request" in problem


def test_a_large_result_needs_a_signed_link_and_no_inline_data() -> None:
    assert S.large_result_problems(tool_reply(*OFFLOADED, link())) == (SIGNED, [])

    uri, problems = S.large_result_problems(tool_reply(*OFFLOADED, link("shopware://tool-result/plain")))
    assert uri == "shopware://tool-result/plain"
    assert "not a signed token" in problems[0]

    assert "inline" in S.large_result_problems(tool_reply(text('{"data": [1]}')))[1][0]
    assert "call failed" in S.large_result_problems(tool_reply(text("boom"), is_error=True))[1][0]


def frame(at: float, method: str = "", **extra: object) -> ListenFrame:
    body: JsonObject = {"jsonrpc": "2.0", **extra}
    if method:
        body["method"] = method
    return ListenFrame(at, body)


ACK = C.ACKNOWLEDGED
CLOSED: JsonObject = {"resultType": "complete"}


def test_an_admin_stream_measured_on_the_lane_passes() -> None:
    """Acknowledged at 0.24 s, the change shortly after the edit, closed at 25.31 s."""
    frames = [frame(0.24, ACK), frame(1.0, S.LIST_CHANGED), frame(25.31, id=1, result=CLOSED)]

    assert S.listen_problems(frames, expect_change=True) == {"ack": None, "change": None, "close": None}


def test_the_store_stream_measured_on_the_lane_fails_twice() -> None:
    """Acknowledged only with the closing result, at 30.3 s: buffered, and at
    the worker limit."""
    problems = S.listen_problems([frame(30.3, ACK), frame(30.3, id=1, result=CLOSED)], expect_change=False)

    assert "buffered" in (problems["ack"] or "")
    assert problems["change"] is None
    assert "max_execution_time" in (problems["close"] or "")


@pytest.mark.parametrize(
    ("frames", "key", "problem"),
    [
        pytest.param([], "ack", "no acknowledgement", id="nothing"),
        pytest.param([frame(0.1, S.LIST_CHANGED), frame(0.2, ACK)], "ack", "first message", id="ack-not-first"),
        pytest.param(
            [frame(0.1, ACK), frame(1, id=1, result=CLOSED)], "change", "no tools/list_changed", id="no-change"
        ),
        pytest.param([frame(0.1, ACK), frame(9, S.LIST_CHANGED)], "change", "8.9 s", id="late-change"),
        pytest.param([frame(0.1, ACK)], "close", "did not close", id="never-closed"),
        pytest.param([ListenFrame(0, {"status": 400})], "close", "not streamed", id="refused"),
    ],
)
def test_each_stream_problem_names_itself(frames: list[ListenFrame], key: str, problem: str) -> None:
    assert problem in (S.listen_problems(frames, expect_change=True)[key] or "")


# --- the whole run, with the network stubbed at the module's seams ---


GOOD_STREAM = [frame(0.2, ACK), frame(1.0, S.LIST_CHANGED), frame(25.0, id=1, result=CLOSED)]
ROWS = '[{"id": 1}]'


class Lane:
    """What the lane answered, keyed by method and tool, as the scenarios see it."""

    def __init__(self, session_id: str | None = None, inline: bool = False) -> None:
        self.session_id: str | None = session_id
        self.inline: bool = inline
        self.listened: list[JsonObject] = []

    def stateless(
        self, method: str, params: JsonObject | None = None, _endpoint: Endpoint | None = None
    ) -> StatelessReply:
        name = (params or {}).get("name")
        if method == "tools/call" and name in S.SESSION_TOOLS:
            return tool_reply(text(HINT), is_error=True, session_id=self.session_id)
        if method == "tools/call" and self.inline:
            return tool_reply(text(ROWS), session_id=self.session_id)
        if method == "tools/call":
            return tool_reply(*OFFLOADED, link(), session_id=self.session_id)
        rows = message({"result": {"contents": [{"uri": SIGNED, "text": ROWS}]}})
        return StatelessReply(200, rows, self.session_id)

    def listen(
        self, _endpoint: Endpoint, notifications: JsonObject, *, on_ack: Callable[[], None] | None, timeout_s: float
    ) -> list[ListenFrame]:
        del timeout_s
        self.listened.append(notifications)
        if on_ack is not None:
            on_ack()
            return GOOD_STREAM
        return GOOD_STREAM[::2]


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, body: JsonObject | None = None) -> JsonObject:
        del body
        self.calls.append((method, path))
        return {}


class Other:
    def __enter__(self) -> Principal:
        return Principal("other", "k", "s", "all", allowlist_path="/api/_action/integration/x/mcp-allowlist")

    def __exit__(self, *_exc: object) -> None:
        return None


def stub_lane(monkeypatch: pytest.MonkeyPatch, lane: Lane, discover: object, delete_status: int = 400) -> FakeApi:
    api = FakeApi()
    monkeypatch.setattr(S, "mcp_discover", const((200, discover)))
    monkeypatch.setattr(S, "mcp_stateless", lane.stateless)
    monkeypatch.setattr(S, "mcp_listen", lane.listen)
    monkeypatch.setattr(S, "mcp_stateless_tools", const(["shopware-tool-search", "a"]))
    monkeypatch.setattr(S, "mcp_init", const(("session", "")))
    toolsets: list[JsonObject] = [{"name": "dev", "tools": ["a"]}, {"name": "empty", "tools": []}]
    monkeypatch.setattr(S, "mcp_toolsets_list", const(toolsets))
    monkeypatch.setattr(
        S, "mcp_tools_list_all", const([{"name": n} for n in ["shopware-tool-search", "shopware-toolsets-list", "a"]])
    )
    monkeypatch.setattr(S, "mcp_read_resource", const(message({"result": {"contents": [{"text": ROWS}]}})))
    monkeypatch.setattr(S.requests, "delete", const(FakeResp(delete_status)))
    monkeypatch.setattr(S, "AdminApi", const(api))
    monkeypatch.setattr(S, "another_principal", const(Other()))
    return api


def test_the_admin_run_on_the_lane_passes_every_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    lane = Lane()
    api = stub_lane(monkeypatch, lane, answer())
    rep = reporter()

    S.verify_stateless_era(rep, ADMIN, provision=True)

    assert rep.failed == 0, rep.records
    assert ("stateless: tools/list_changed arrives through subscriptions/listen (admin)", "pass") in records(rep)
    assert api.calls == [("POST", "/api/_action/integration/x/mcp-allowlist")]
    assert lane.listened == [{"toolsListChanged": True}]


def test_the_store_run_skips_what_the_store_cannot_do(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = stub_lane(monkeypatch, Lane(), answer(listchanged=False))
    rep = reporter()

    S.verify_stateless_era(rep, STORE)

    assert rep.failed == 0, rep.records
    skipped = {label for label, status in records(rep) if status == "skipped"}
    assert skipped == {
        "stateless: a large result is a signed resource_link (store)",
        "stateless: tools/list_changed arrives through subscriptions/listen (store)",
    }


def test_admin_without_provisioning_skips_the_change_and_still_checks_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = stub_lane(monkeypatch, Lane(), answer())
    rep = reporter()

    S.verify_stateless_era(rep, ADMIN)

    assert ("stateless: tools/list_changed arrives through subscriptions/listen (admin)", "skipped") in records(rep)
    assert ("stateless: subscriptions/listen acknowledges at once (admin)", "pass") in records(rep)


def test_a_minted_session_a_wrong_delete_and_no_listchanged_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = stub_lane(monkeypatch, Lane(session_id="abc"), answer(listchanged=False), delete_status=405)
    rep = reporter()

    S.verify_stateless_era(rep, ADMIN, provision=True)

    failed = {label for label, status in records(rep) if status == "fail"}
    assert failed == {
        "stateless: no Mcp-Session-Id is minted (admin)",
        "stateless: DELETE without a session answers 400 (admin)",
        "stateless: tools/list_changed arrives through subscriptions/listen (admin)",
    }


def test_a_provisioning_failure_is_named_and_the_stream_still_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = stub_lane(monkeypatch, Lane(), answer())

    def refuse(*_args: object) -> FakeApi:
        raise S.ProvisioningFailed("no token")

    monkeypatch.setattr(S, "AdminApi", refuse)
    rep = reporter()

    S.verify_stateless_era(rep, ADMIN, provision=True)

    assert ("stateless: tools/list_changed arrives through subscriptions/listen (admin)", "fail") in records(rep)
    assert ("stateless: subscriptions/listen closes before the worker limit (admin)", "pass") in records(rep)


def test_a_lane_without_a_toolset_to_pin_skips_that_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = stub_lane(monkeypatch, Lane(), answer(listchanged=False))
    monkeypatch.setattr(S, "mcp_toolsets_list", const([]))
    rep = reporter()

    S.verify_stateless_era(rep, STORE)

    assert ("stateless: ?toolsets= pins the same tools as the handshake era (store)", "skipped") in records(rep)


def test_an_inline_large_result_stops_before_reading_it_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _ = stub_lane(monkeypatch, Lane(inline=True), answer())
    rep = reporter()

    S.verify_stateless_era(rep, ADMIN)

    labels = [label for label, _ in records(rep)]
    assert ("stateless: a large result is a signed resource_link (admin)", "fail") in records(rep)
    assert "stateless: the next stateless request of the principal reads it" not in labels
