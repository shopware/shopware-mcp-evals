"""The MCP HTTP client: response parsing, throttle retry, pagination, and the
session/toolset/prompt calls every runner is built on.

Everything is driven through a fake `requests.post`, so these cover the transport
edge cases that only show up against a real server: an SSE body carrying a
notification alongside the response, a cursor that repeats a tool, a tool-level
error that is not a protocol error.
"""

import json

import pytest
import requests

import mcp_client as C


class FakeResp:
    def __init__(self, status_code, *, headers=None, body=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)


# --- response parsing: application/json object, SSE stream, and (defensive) array ---
SSE_BODY = (
    "event: message\n"
    'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n'
    "\n"
    "event: message\n"
    'data: {"jsonrpc":"2.0","id":4,"result":{"tools":[{"name":"x"}]}}\n'
    "\n"
)


def test_parse_sse_extracts_all_messages():
    msgs = C._parse_sse(SSE_BODY)
    assert len(msgs) == 2
    assert msgs[0]["method"] == "notifications/tools/list_changed"
    assert msgs[1]["id"] == 4


def test_pick_returns_matching_id():
    msgs = [{"method": "notifications/tools/list_changed"}, {"id": 4, "result": {"ok": True}}]
    assert C._pick(msgs, 4)["result"] == {"ok": True}


def test_pick_empty_when_no_match():
    assert C._pick([{"method": "notifications/x"}], 4) == {}


def test_response_json_object():
    resp = FakeResp(
        200, headers={"Content-Type": "application/json"}, body={"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    )
    assert C._response(resp, 2)["result"] == {"tools": []}


def test_response_sse_stream_picks_response_over_notification():
    resp = FakeResp(200, headers={"Content-Type": "text/event-stream; charset=UTF-8"}, text=SSE_BODY)
    assert C._response(resp, 4)["result"]["tools"] == [{"name": "x"}]


def test_response_tolerates_json_array():
    resp = FakeResp(
        200,
        headers={"Content-Type": "application/json"},
        body=[{"method": "notifications/x"}, {"id": 4, "result": {"ok": 1}}],
    )
    assert C._response(resp, 4)["result"] == {"ok": 1}


def test_throttle_wait_from_retry_after_header():
    assert C._throttle_wait(FakeResp(429, headers={"Retry-After": "16"})) == 16.0


def test_throttle_wait_caps_retry_after():
    assert C._throttle_wait(FakeResp(429, headers={"Retry-After": "999"})) == C.THROTTLE_MAX_WAIT_S


def test_throttle_wait_parses_body_hint():
    resp = FakeResp(429, body={"error": {"message": "MCP endpoint throttled for 8 seconds."}})
    assert C._throttle_wait(resp) == 8.0


def test_throttle_wait_caps_body_hint():
    resp = FakeResp(429, body={"error": {"message": "throttled for 999 seconds"}})
    assert C._throttle_wait(resp) == C.THROTTLE_MAX_WAIT_S


def test_throttle_wait_default_when_no_hint():
    assert C._throttle_wait(FakeResp(429, body={})) == 5.0


def test_rpc_retries_then_succeeds(monkeypatch):
    throttled = FakeResp(429, body={"error": {"message": "throttled for 1 seconds"}})
    ok = FakeResp(200, body={"result": {}})
    responses = [throttled, throttled, ok]
    calls = {"post": 0, "sleep": 0}

    def fake_post(*_a, **_k):
        calls["post"] += 1
        return responses.pop(0)

    monkeypatch.setattr(C.requests, "post", fake_post)
    monkeypatch.setattr(C.time, "sleep", lambda _s: calls.__setitem__("sleep", calls["sleep"] + 1))

    resp = C._rpc("tools/list", {})
    assert resp is ok
    assert calls["post"] == 3
    assert calls["sleep"] == 2  # slept before each retry


def test_rpc_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(
        C.requests, "post", lambda *_a, **_k: FakeResp(429, body={"error": {"message": "throttled for 1 seconds"}})
    )
    monkeypatch.setattr(C.time, "sleep", lambda _s: None)

    with pytest.raises(requests.exceptions.HTTPError):
        C._rpc("tools/list", {})


# ---------------------------------------------------------------------------
# Endpoint construction
# ---------------------------------------------------------------------------
# ADMIN/STORE are built once at import from the process environment. That is
# right for the runners — the Store token in particular must stay stable for the
# whole run or a second endpoint means a second cart — but it left no way to
# point at a different server, so these factories exist alongside the constants.


def test_admin_endpoint_defaults_to_the_process_configuration():
    assert C.admin_endpoint().url == C.ADMIN.url
    assert C.admin_endpoint().auth_headers == C.ADMIN.auth_headers


def test_endpoint_can_target_another_server_without_touching_the_environment():
    ep = C.admin_endpoint(access_key="k", secret_access_key="s", base_url="http://other:9000")

    assert ep.url == "http://other:9000/api/_mcp"
    assert ep.auth_headers["sw-access-key"] == "k"
    assert ep.auth_headers["sw-secret-access-key"] == "s"
    # The module-level default is unchanged — no global was mutated.
    assert C.ADMIN.url != ep.url


def test_base_url_trailing_slash_does_not_double_up():
    assert C.admin_endpoint(base_url="http://x:8000/").url == "http://x:8000/api/_mcp"


def test_every_endpoint_carries_the_json_content_type():
    for ep in (C.admin_endpoint(), C.store_endpoint()):
        assert ep.auth_headers["Content-Type"] == "application/json"


def test_store_endpoints_get_distinct_context_tokens():
    """Two endpoints mean two carts; that is why the runner reuses one STORE."""
    a, b = C.store_endpoint(), C.store_endpoint()

    assert a.auth_headers["sw-context-token"] != b.auth_headers["sw-context-token"]


def test_store_context_token_can_be_pinned():
    ep = C.store_endpoint(context_token="fixed-token")

    assert ep.auth_headers["sw-context-token"] == "fixed-token"


def test_endpoint_by_name_returns_the_shared_defaults_not_fresh_ones():
    """A runner that rebuilt its endpoint mid-run would lose the cart it had
    been filling, so lookup must hand back the same object every time."""
    assert C.endpoint_by_name("store") is C.STORE
    assert C.endpoint_by_name("admin") is C.ADMIN


# ---------------------------------------------------------------------------
# Session init
# ---------------------------------------------------------------------------
def json_resp(body, headers=None):
    return FakeResp(200, headers={"Content-Type": "application/json", **(headers or {})}, body=body)


def test_init_returns_the_session_id_and_server_instructions(monkeypatch):
    monkeypatch.setattr(
        C.requests,
        "post",
        lambda *_a, **_k: json_resp(
            {"jsonrpc": "2.0", "id": 1, "result": {"instructions": "Use entity tools."}},
            headers={"Mcp-Session-Id": "sid-1"},
        ),
    )

    assert C.mcp_init() == ("sid-1", "Use entity tools.")


def test_init_without_a_session_header_is_fatal(monkeypatch):
    """Every later call is keyed by Mcp-Session-Id; continuing without one would
    silently run each request in a fresh session."""
    monkeypatch.setattr(C.requests, "post", lambda *_a, **_k: json_resp({"jsonrpc": "2.0", "id": 1, "result": {}}))

    with pytest.raises(RuntimeError, match="No Mcp-Session-Id"):
        C.mcp_init()


def test_init_tolerates_a_server_that_sends_no_instructions(monkeypatch):
    monkeypatch.setattr(
        C.requests,
        "post",
        lambda *_a, **_k: json_resp({"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"Mcp-Session-Id": "s"}),
    )

    assert C.mcp_init() == ("s", "")


# ---------------------------------------------------------------------------
# Result / error extraction
# ---------------------------------------------------------------------------
def test_result_text_takes_the_first_text_block():
    resp = {"result": {"content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]}}

    assert C.mcp_result_text(resp) == "first"


def test_result_text_skips_a_non_text_block():
    resp = {"result": {"content": [{"type": "image", "data": "..."}, {"type": "text", "text": "wanted"}]}}

    assert C.mcp_result_text(resp) == "wanted"


def test_result_text_treats_a_block_without_a_type_as_text():
    assert C.mcp_result_text({"result": {"content": [{"text": "implicit"}]}}) == "implicit"


def test_result_text_of_an_empty_response_is_empty():
    assert C.mcp_result_text({}) == ""
    assert C.mcp_result_text({"result": {"content": []}}) == ""


def test_result_meta_defaults_to_a_dict_even_when_null():
    assert C.mcp_result_meta({"result": {"_meta": None}}) == {}
    assert C.mcp_result_meta({"result": {"_meta": {"listChanged": True}}}) == {"listChanged": True}


def test_call_error_reports_a_protocol_error():
    assert C.mcp_call_error({"error": {"message": "Tool not found"}}) == "Tool not found"


def test_call_error_names_an_unlabelled_protocol_error():
    assert C.mcp_call_error({"error": {}}) == "unknown error"


def test_call_error_reports_a_tool_level_error_from_the_text_block():
    """isError puts the message in the content, not in error.message — missing
    this reads a failed tool call as a success."""
    resp = {"result": {"isError": True, "content": [{"type": "text", "text": "entity not found"}]}}

    assert C.mcp_call_error(resp) == "entity not found"


def test_call_error_falls_back_when_an_is_error_response_has_no_text():
    assert C.mcp_call_error({"result": {"isError": True, "content": []}}) == "tool error"


def test_call_error_of_a_successful_response_is_empty():
    assert C.mcp_call_error({"result": {"content": [{"type": "text", "text": "ok"}]}}) == ""


# ---------------------------------------------------------------------------
# tools/list pagination
# ---------------------------------------------------------------------------
def paginated(monkeypatch, pages):
    """Serve `pages` in order, one per tools/list request."""
    calls = {"n": 0}

    def post(*_a, **kwargs):
        page = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return json_resp({"jsonrpc": "2.0", "id": 2, "result": page})

    monkeypatch.setattr(C.requests, "post", post)
    return calls


def test_tools_list_follows_next_cursor_across_pages(monkeypatch):
    calls = paginated(
        monkeypatch,
        [
            {"tools": [{"name": "a"}, {"name": "b"}], "nextCursor": "p2"},
            {"tools": [{"name": "c"}]},
        ],
    )

    tools = C.mcp_tools_list_all("sid")

    assert [t["name"] for t in tools] == ["a", "b", "c"]
    assert calls["n"] == 2


def test_tools_list_stops_on_a_single_page(monkeypatch):
    calls = paginated(monkeypatch, [{"tools": [{"name": "a"}]}])

    assert len(C.mcp_tools_list_all("sid")) == 1
    assert calls["n"] == 1


def test_tools_list_treats_an_empty_next_cursor_as_the_end(monkeypatch):
    calls = paginated(monkeypatch, [{"tools": [{"name": "a"}], "nextCursor": ""}])

    C.mcp_tools_list_all("sid")

    assert calls["n"] == 1, "an empty cursor string must not trigger another page"


def test_tools_list_rejects_a_tool_repeated_across_pages(monkeypatch):
    """A server that repeats a tool is paginating wrong; silently de-duplicating
    it would hide the bug and undercount the catalogue."""
    paginated(
        monkeypatch,
        [{"tools": [{"name": "a"}], "nextCursor": "p2"}, {"tools": [{"name": "a"}]}],
    )

    with pytest.raises(RuntimeError, match="Duplicate tool 'a'"):
        C.mcp_tools_list_all("sid")


def test_tools_list_gives_up_on_a_cursor_that_never_ends(monkeypatch):
    """Without the guard a server that always returns a cursor loops forever."""
    n = {"i": 0}

    def post(*_a, **_k):
        n["i"] += 1
        return json_resp({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": f"t{n['i']}"}], "nextCursor": "c"}})

    monkeypatch.setattr(C.requests, "post", post)

    with pytest.raises(RuntimeError, match="did not terminate within 50 pages"):
        C.mcp_tools_list_all("sid")
    assert n["i"] == 50


def test_tools_list_of_an_empty_surface_is_an_empty_list(monkeypatch):
    paginated(monkeypatch, [{"tools": []}])

    assert C.mcp_tools_list_all("sid") == []


# ---------------------------------------------------------------------------
# Toolsets
# ---------------------------------------------------------------------------
def tool_call_resp(monkeypatch, payload=None, error=None, capture=None):
    def post(_url, **kwargs):
        if capture is not None:
            capture.append(kwargs["json"])
        if error:
            return json_resp({"jsonrpc": "2.0", "id": 99, "error": {"message": error}})
        body = {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}, "jsonrpc": "2.0", "id": 99}
        return json_resp(body)

    monkeypatch.setattr(C.requests, "post", post)


def test_toolsets_list_parses_the_payload(monkeypatch):
    tool_call_resp(monkeypatch, {"data": {"toolsets": [{"name": "entity", "enabled": False}]}})

    assert C.mcp_toolsets_list("sid") == [{"name": "entity", "enabled": False}]


def test_toolsets_list_raises_when_the_meta_tool_itself_fails(monkeypatch):
    tool_call_resp(monkeypatch, error="Tool not found")

    with pytest.raises(RuntimeError, match="shopware-toolsets-list failed: Tool not found"):
        C.mcp_toolsets_list("sid")


def test_toolsets_list_of_a_payload_without_toolsets_is_empty(monkeypatch):
    tool_call_resp(monkeypatch, {"data": {}})

    assert C.mcp_toolsets_list("sid") == []


def test_enable_toolset_sends_the_toolset_name(monkeypatch):
    sent = []
    tool_call_resp(monkeypatch, {"success": True}, capture=sent)

    C.enable_toolset("sid", "entity")

    assert sent[0]["params"] == {"name": "shopware-toolset-enable", "arguments": {"toolset": "entity"}}


def test_enable_all_toolsets_skips_the_ones_already_enabled(monkeypatch):
    """Re-enabling is wasted round trips against a throttled endpoint."""
    sent = []

    def post(_url, **kwargs):
        body = kwargs["json"]
        sent.append(body["params"].get("name"))
        if body["params"].get("name") == "shopware-toolsets-list":
            payload = {"data": {"toolsets": [{"name": "a", "enabled": True}, {"name": "b", "enabled": False}]}}
        else:
            payload = {"success": True}
        return json_resp(
            {"jsonrpc": "2.0", "id": 99, "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
        )

    monkeypatch.setattr(C.requests, "post", post)

    assert C.enable_all_toolsets("sid") == ["a", "b"]
    assert sent.count("shopware-toolset-enable") == 1, "only the disabled toolset needs a call"


def test_enable_all_toolsets_names_the_toolset_that_failed(monkeypatch):
    def post(_url, **kwargs):
        if kwargs["json"]["params"].get("name") == "shopware-toolsets-list":
            payload = {"data": {"toolsets": [{"name": "broken", "enabled": False}]}}
            return json_resp(
                {"jsonrpc": "2.0", "id": 99, "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
            )
        return json_resp({"jsonrpc": "2.0", "id": 99, "error": {"message": "unknown toolset"}})

    monkeypatch.setattr(C.requests, "post", post)

    with pytest.raises(RuntimeError, match="Failed to enable toolset 'broken': unknown toolset"):
        C.enable_all_toolsets("sid")


# ---------------------------------------------------------------------------
# System prompt assembly
# ---------------------------------------------------------------------------
def prompts_server(monkeypatch, prompts, bodies):
    def post(_url, **kwargs):
        body = kwargs["json"]
        if body["method"] == "prompts/list":
            result = {"prompts": [{"name": n} for n in prompts]}
        else:
            result = bodies[body["params"]["name"]]
        return json_resp({"jsonrpc": "2.0", "id": body["id"], "result": result})

    monkeypatch.setattr(C.requests, "post", post)


def test_system_prompt_joins_instructions_and_every_context_prompt(monkeypatch):
    prompts_server(
        monkeypatch,
        ["ctx-a", "ctx-b"],
        {
            "ctx-a": {"messages": [{"content": {"text": "A body"}}]},
            "ctx-b": {"messages": [{"content": {"text": "B body"}}]},
        },
    )

    out = C.mcp_fetch_system_prompt("sid", "Server says hello")

    assert out == "Server says hello\n\n---\n\nA body\n\n---\n\nB body"


def test_system_prompt_omits_absent_server_instructions(monkeypatch):
    prompts_server(monkeypatch, ["ctx"], {"ctx": {"messages": [{"content": {"text": "only body"}}]}})

    assert C.mcp_fetch_system_prompt("sid", "") == "only body"


def test_system_prompt_drops_blank_prompt_bodies(monkeypatch):
    """A registered prompt with no text would otherwise contribute an empty
    section and a stray separator."""
    prompts_server(monkeypatch, ["empty"], {"empty": {"messages": [{"content": {"text": "   "}}]}})

    assert C.mcp_fetch_system_prompt("sid", "instructions") == "instructions"


def test_system_prompt_accepts_a_non_dict_content_block(monkeypatch):
    prompts_server(monkeypatch, ["odd"], {"odd": {"messages": [{"content": "bare string"}]}})

    assert "bare string" in C.mcp_fetch_system_prompt("sid", "")


def test_system_prompt_with_no_prompts_registered_is_just_the_instructions(monkeypatch):
    prompts_server(monkeypatch, [], {})

    assert C.mcp_fetch_system_prompt("sid", "instructions only") == "instructions only"


# ---------------------------------------------------------------------------
# SSE parsing and env loading — the remaining transport edges
# ---------------------------------------------------------------------------
def test_sse_parses_an_event_terminated_by_a_blank_line():
    body = 'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'

    assert C._parse_sse(body) == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}]


def test_sse_parses_a_trailing_event_with_no_blank_line():
    """The server does not always terminate the last event, and dropping it would
    lose the response while keeping the notification."""
    assert C._parse_sse('data: {"id":1}') == [{"id": 1}]


def test_sse_joins_a_payload_split_across_data_lines():
    assert C._parse_sse('data: {"id":\ndata: 1}\n\n') == [{"id": 1}]


def test_sse_skips_a_malformed_event_rather_than_failing_the_call():
    body = 'data: not json\n\ndata: {"id":2}\n\n'

    assert C._parse_sse(body) == [{"id": 2}]


def test_sse_skips_a_malformed_trailing_event():
    assert C._parse_sse('data: {"id":1}\n\ndata: {oops') == [{"id": 1}]


def test_sse_ignores_non_data_lines():
    body = 'event: message\nid: 7\ndata: {"id":1}\n\n'

    assert C._parse_sse(body) == [{"id": 1}]


def test_response_picks_the_reply_and_ignores_a_pushed_notification():
    """After a toolset-enable the server pushes tools/list_changed alongside the
    response; matching on id is what keeps them apart."""
    body = (
        'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n\n'
        'data: {"jsonrpc":"2.0","id":99,"result":{"ok":1}}\n\n'
    )
    resp = FakeResp(200, headers={"Content-Type": "text/event-stream"}, text=body)

    assert C._response(resp, 99) == {"jsonrpc": "2.0", "id": 99, "result": {"ok": 1}}


def test_response_is_empty_when_no_message_matches_the_id():
    resp = FakeResp(200, headers={"Content-Type": "text/event-stream"}, text='data: {"id":1}\n\n')

    assert C._response(resp, 99) == {}


def test_response_tolerates_a_top_level_json_array():
    """A spec-removed batch shape, accepted defensively."""
    resp = FakeResp(200, headers={"Content-Type": "application/json"}, body=[{"id": 5, "result": {}}])

    assert C._response(resp, 5) == {"id": 5, "result": {}}


def test_response_of_a_non_object_json_body_is_empty():
    resp = FakeResp(200, headers={"Content-Type": "application/json"}, body="just a string")

    assert C._response(resp, 1) == {}


def test_throttle_wait_falls_back_when_neither_hint_is_parseable():
    assert C._throttle_wait(FakeResp(429, body={"error": {"message": "slow down"}})) == 5.0


def test_throttle_wait_survives_a_body_that_is_not_json():
    """raise_for_status has not run yet, so a proxy's HTML 429 page reaches here."""

    class NoJson(FakeResp):
        def json(self):
            raise ValueError("not json")

    assert C._throttle_wait(NoJson(429)) == 5.0


def test_load_env_is_a_no_op_without_an_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "BASE", tmp_path)

    C.load_env()  # must not raise
