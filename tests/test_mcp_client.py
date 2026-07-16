"""Unit tests for the mcp_client throttle-retry logic."""

import pytest
import requests

import mcp_client as C


class FakeResp:
    def __init__(self, status_code, *, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)


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
