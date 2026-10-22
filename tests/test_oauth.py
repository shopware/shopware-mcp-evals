"""The MCP OAuth flow against a fake shop: skipped without discovery, gaps known.

The fake answers the way the trunk lane on shopware/shopware integration/mcp-iteration-4
answered the scripted flow, with switches for what older or fixed shops do
differently: no discovery at all (before #21862), no registered client, and the
two gaps closed.
"""

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from eval.result_schema import JsonObject, as_object
from functional import oauth as O
from functional.reporting import Reporter
from mcp_client import Endpoint, admin_endpoint

BASE = "http://shop.example"
MCP = f"{BASE}/api/_mcp"
ISSUER = f"{BASE}/api"
ADMIN: Endpoint = admin_endpoint("SWUAadmin", "secret", base_url=BASE)
LOGIN_SNIPPETS: JsonObject = {"en-GB": {"global": {}, "sw-login": {}}}
ALL_SNIPPETS: JsonObject = {"en-GB": {"global": {}, "sw-login": {}, "sw-product": {}, "sw-order": {}}}


def jwt(aud: list[str]) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"aud": aud}).encode()).rstrip(b"=").decode()
    return f"header.{claims}.signature"


def reply(status: int, body: object = None, **headers: str) -> O.Reply:
    text = body if isinstance(body, str) else ("" if body is None else json.dumps(body))
    return O.Reply(status, {k.lower().replace("_", "-"): v for k, v in headers.items()}, text)


class FakeShop:
    """Just enough of Shopware's OAuth and MCP endpoints for the flow."""

    def __init__(self, *, discovery: bool = True, registered: bool = True, gaps: bool = True) -> None:
        self.discovery: bool = discovery
        self.registered: bool = registered
        self.gaps: bool = gaps
        self.codes: dict[str, tuple[str, str]] = {}
        self.refresh: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []

    def bound(self, headers: Mapping[str, str]) -> bool:
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        return MCP in O.jwt_audience(token)

    def authed(self, headers: Mapping[str, str]) -> bool:
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        return token == "admin-token" or (token.count(".") == 2 and not self.bound(headers))

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        parts = urlsplit(url)
        path, query = parts.path, dict(parse_qsl(parts.query))
        self.requests.append((method, path))
        text = (body or b"").decode()
        form: dict[str, str] = (
            dict(parse_qsl(text))
            if headers.get("Content-Type") == "application/x-www-form-urlencoded"
            else {k: str(v) for k, v in as_object(cast(object, json.loads(text) if text else {})).items()}
        )
        if path == "/api/_mcp":
            return self.mcp(method, headers, text)
        if path == "/store-api/_mcp":
            return reply(401, "Unauthorized")
        if path == "/.well-known/oauth-protected-resource/api/_mcp":
            if not self.discovery:
                return reply(404, "Not Found")
            return reply(200, {"resource": MCP, "authorization_servers": [ISSUER]})
        if path == "/.well-known/oauth-authorization-server/api":
            return reply(200, {"issuer": ISSUER, "code_challenge_methods_supported": ["S256"]})
        if path == "/api/oauth/authorize":
            return self.authorize(method, query, form)
        if path == "/api/oauth/token":
            return self.token(form)
        if path in ("/api/_info/me", "/api/_info/version", "/api/search/product"):
            return reply(200, {}) if self.authed(headers) else reply(401, {"errors": []})
        if path == "/api/_info/system-health-check":
            leaks = self.gaps and self.bound(headers)
            return reply(200, {"checks": []}) if self.authed(headers) or leaks else reply(401, {"errors": []})
        if path == "/api/_admin/snippets":
            full = self.authed(headers) or (self.gaps and self.bound(headers))
            return reply(200, ALL_SNIPPETS if full else LOGIN_SNIPPETS)
        return reply(404, "Not Found")

    def mcp(self, method: str, headers: Mapping[str, str], text: str) -> O.Reply:
        if method == "DELETE":
            return reply(204)
        auth = headers.get("Authorization", "")
        if "sw-access-key" in headers or (auth and not self.bound(headers)):
            return reply(401, "Unauthorized", WWW_Authenticate='Bearer error="invalid_token"')
        if not auth:
            challenge = f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/api/_mcp"'
            return reply(401, "Unauthorized", **({"WWW_Authenticate": challenge} if self.discovery else {}))
        rpc = as_object(cast(object, json.loads(text)))
        if rpc.get("method") == "initialize":
            return reply(200, {"jsonrpc": "2.0", "id": 1, "result": {}}, Mcp_Session_Id="s-1")
        if rpc.get("method") == "tools/list":
            return reply(200, 'event: message\ndata: {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "a"}]}}')
        if rpc.get("method") == "tools/call":
            return reply(200, {"jsonrpc": "2.0", "id": 3, "result": {"content": []}})
        return reply(202)

    def authorize(self, method: str, query: Mapping[str, str], form: Mapping[str, str]) -> O.Reply:
        if method == "GET":
            if not self.registered:
                return reply(403, {"errors": [{"code": "FRAMEWORK__API_INVALID_ACCESS_KEY"}]})
            if urlsplit(query["redirect_uri"]).hostname != "127.0.0.1":
                return reply(400, {"errors": [{"code": "FRAMEWORK__OAUTH_INVALID_REDIRECT_URI"}]})
            if query.get("resource", MCP) != MCP:
                return reply(302, Location=f"{query['redirect_uri']}?error=invalid_target")
            return reply(302, Location=f"{BASE}/admin#/oauth/authorize?{urlencode(query)}")
        redirect = form["redirect_uri"]
        if form["approved"] != "True":
            return reply(200, {"redirectUri": f"{redirect}?error=access_denied&iss={ISSUER}"})
        code = f"code-{len(self.codes)}"
        self.codes[code] = (form.get("resource", ""), form["code_challenge"])
        return reply(
            200, {"redirectUri": f"{redirect}?{urlencode({'code': code, 'state': form['state'], 'iss': ISSUER})}"}
        )

    def token(self, form: Mapping[str, str]) -> O.Reply:
        grant = form["grant_type"]
        if grant == "client_credentials":
            return reply(200, {"access_token": "admin-token"})
        if grant == "refresh_token":
            resource = self.refresh.get(form["refresh_token"])
            if resource is None or form.get("resource", resource) != resource:
                return reply(400, {"error": "invalid_grant"})
            return self.issue(form["client_id"], resource)
        resource, challenge = self.codes.pop(form["code"])
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
            return reply(400, {"error": "invalid_grant"})
        if form.get("resource", resource) != resource:
            return reply(400, {"error": "invalid_target"})
        return self.issue(form["client_id"], resource)

    def issue(self, client: str, resource: str) -> O.Reply:
        refresh = f"rt-{len(self.refresh)}"
        self.refresh[refresh] = resource
        aud = [client, resource] if resource else [client]
        return reply(200, {"access_token": jwt(aud), "refresh_token": refresh})


def run(shop: FakeShop, monkeypatch: pytest.MonkeyPatch, client: str = "") -> Reporter:
    if client:
        monkeypatch.setenv(O.CLIENT_ENV, client)
    else:
        monkeypatch.delenv(O.CLIENT_ENV, raising=False)
    rep = Reporter(BASE, color=False)
    O.verify_oauth_flow(rep, ADMIN, send=shop)
    return rep


def failures(rep: Reporter) -> list[str]:
    return [r["label"] for r in rep.records if r["status"] == "fail"]


def test_a_shop_without_discovery_is_one_skip_naming_the_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(discovery=False), monkeypatch)

    assert (rep.passed, rep.failed, rep.skipped) == (0, 0, 1)
    [record] = rep.records
    assert record["label"] == O.FLOW_CHECK and record["status"] == "skipped"
    assert "shopware/shopware#21862" in record.get("reason", "")


def test_the_whole_flow_passes_on_the_lane_with_both_gaps_known(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(), monkeypatch)

    assert failures(rep) == [O.HEALTH_CHECK_GAP, O.SNIPPETS_GAP]
    assert rep.expected_failed == 2 and rep.exit_code == 0
    assert rep.passed == 35 and rep.skipped == 0
    [notice] = rep.annotations("Admin")
    assert notice.startswith("::notice") and O.FIX_URL in notice


def test_closing_the_gaps_asks_for_the_entries_to_go(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(gaps=False), monkeypatch)

    assert rep.failed == 0 and rep.passed == 37
    assert [label for label, _ in rep.recovered] == [O.HEALTH_CHECK_GAP, O.SNIPPETS_GAP]
    [warning] = rep.annotations("Admin")
    assert warning.startswith("::warning") and "2 known failure(s) passed" in warning


def test_an_unregistered_client_skips_after_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(registered=False), monkeypatch, client="someone-else")

    skip = rep.records[-1]
    assert skip["status"] == "skipped" and "'someone-else'" in skip.get("reason", "")
    assert O.CONFIG_FILE in skip.get("reason", "")
    assert rep.failed == 0


def test_the_client_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(gaps=False), monkeypatch, client="claude-code")

    assert rep.failed == 0, failures(rep)


def test_a_gap_failing_another_way_is_not_the_known_one() -> None:
    """A 500 on the health check is new, not the gap the registry describes."""
    shop = FakeShop(gaps=False)
    real = shop.__call__

    def broken(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        return reply(500, "boom") if url.endswith("/system-health-check") else real(method, url, headers, body)

    rep = Reporter(BASE, color=False)
    O.verify_oauth_flow(rep, ADMIN, send=broken)

    assert failures(rep) == [O.HEALTH_CHECK_GAP]
    assert rep.unexpected_failed == 1


def test_a_failed_admin_sign_in_stops_the_flow() -> None:
    shop = FakeShop()
    real = shop.__call__

    def refusing(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        if b"client_credentials" in (body or b""):
            return reply(401, {"errors": []})
        return real(method, url, headers, body)

    rep = Reporter(BASE, color=False)
    O.verify_oauth_flow(rep, ADMIN, send=refusing)

    assert failures(rep) == ["administrator token for the consent decision"]
    assert ("GET", "/api/oauth/authorize") not in shop.requests


def test_a_refused_code_exchange_stops_before_the_mcp_checks() -> None:
    shop = FakeShop()
    real = shop.__call__

    def refusing(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        if b"authorization_code" in (body or b""):
            return reply(400, {"error": "invalid_grant"})
        return real(method, url, headers, body)

    rep = Reporter(BASE, color=False)
    O.verify_oauth_flow(rep, ADMIN, send=refusing)

    assert failures(rep) == ["code, PKCE and resource exchanged for a token"]
    assert ("POST", "/api/_mcp") in shop.requests  # discovery only


def test_no_refresh_token_is_a_failure_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    shop = FakeShop(gaps=False)
    real = shop.issue

    def without_refresh(client: str, resource: str) -> O.Reply:
        issued = real(client, resource).json()
        return reply(200, {"access_token": issued["access_token"]})

    monkeypatch.setattr(shop, "issue", without_refresh)
    rep = run(shop, monkeypatch)

    assert failures(rep) == ["refresh token issued"]


def test_discovery_without_the_challenge_still_runs_and_fails_the_challenge() -> None:
    """The metadata alone is enough to know the shop has the flow."""
    shop = FakeShop(gaps=False)
    real = shop.__call__

    def no_challenge(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        answer = real(method, url, headers, body)
        if url == MCP and "Authorization" not in headers and "sw-access-key" not in headers:
            return O.Reply(answer.status, {}, answer.text)
        return answer

    rep = Reporter(BASE, color=False)
    O.verify_oauth_flow(rep, ADMIN, send=no_challenge)

    assert failures(rep) == ["challenge names resource_metadata and no error code"]


@pytest.mark.parametrize(
    ("token", "aud"),
    [
        pytest.param(jwt(["c", MCP]), ["c", MCP], id="list"),
        pytest.param("h." + base64.urlsafe_b64encode(b'{"aud": "c"}').decode().rstrip("=") + ".s", ["c"], id="string"),
        pytest.param("h." + base64.urlsafe_b64encode(b"{}").decode().rstrip("=") + ".s", [], id="none"),
        pytest.param("not-a-jwt", [], id="not-a-jwt"),
        pytest.param("h.!!!.s", [], id="garbage-claims"),
    ],
)
def test_jwt_audience(token: str, aud: list[str]) -> None:
    assert O.jwt_audience(token) == aud


def test_metadata_urls() -> None:
    assert O.resource_metadata_url('Bearer resource_metadata="https://x/m", scope="a"') == "https://x/m"
    assert O.resource_metadata_url('Bearer error="invalid_token"') == ""
    assert O.well_known_metadata_url(MCP) == f"{BASE}/.well-known/oauth-protected-resource/api/_mcp"
    assert O.authorization_server_metadata_url(ISSUER) == f"{BASE}/.well-known/oauth-authorization-server/api"


def test_mcp_body_reads_json_and_sse() -> None:
    assert O.mcp_body('{"id": 1}') == {"id": 1}
    assert O.mcp_body('event: message\ndata: {"id": 2}\n\n') == {"id": 2}
    assert O.mcp_body("not json") == {}
    assert reply(200, "not json").json() == {}


def test_send_http_does_not_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Resp:
        status_code: int = 302
        headers: dict[str, str] = {"Location": "http://x/admin"}
        text: str = ""

    def request(method: str, url: str, **kwargs: object) -> Resp:
        seen.update(kwargs, method=method, url=url)
        return Resp()

    monkeypatch.setattr(O.requests, "request", request)
    answer = O.send_http("GET", "http://x/a", {"A": "b"}, None)

    assert answer.header("location") == "http://x/admin"
    assert seen["allow_redirects"] is False and seen["timeout"] == O.TIMEOUT_S
