"""Unit tests for the mcp_client response parsing and throttle-retry logic."""

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
