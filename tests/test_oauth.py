"""The MCP OAuth flow against a fake shop: skipped without discovery, passing on #21862.

The fake answers the way the trunk lane on shopware/shopware integration/mcp-iteration-4
answered the flow, with switches for what older shops do differently: no
discovery at all (before #21862), no registered client, and the first draft of
#21862, whose system health check and admin snippets still honoured an MCP-bound
token. Users who sign in with a password are held to the allowlist they were
given, as `McpAllowlistProvider` does for a bearer token.
"""

import base64
import hashlib
import json
from collections.abc import Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import cast, override
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from eval.result_schema import JsonObject, as_object
from functional import oauth as O
from functional.principals import OAUTH_TOOLS, ProvisioningFailed, SignInUser
from functional.reporting import Reporter
from mcp_client import META_TOOLS, Endpoint, admin_endpoint

BASE = "http://shop.example"
MCP = f"{BASE}/api/_mcp"
ISSUER = f"{BASE}/api"
ADMIN: Endpoint = admin_endpoint("SWUAadmin", "secret", base_url=BASE)
LOGIN_SNIPPETS: JsonObject = {"en-GB": {"global": {}, "sw-login": {}}}
ALL_SNIPPETS: JsonObject = {"en-GB": {"global": {}, "sw-login": {}, "sw-product": {}, "sw-order": {}}}


ALLOWLISTED = SignInUser("allowlisted", "mcp-evals-x-oauth-allowlisted", "pw-1", OAUTH_TOOLS)
UNSET = SignInUser("unset", "mcp-evals-x-oauth-unset", "pw-2", None)
EVERYTHING = sorted({*META_TOOLS, *OAUTH_TOOLS, O.REFUSED_TOOL, "shopware-order-state"})


def jwt(aud: list[str], sub: str = "admin") -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"aud": aud, "sub": sub}).encode()).rstrip(b"=").decode()
    return f"header.{claims}.signature"


def subject(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    claims = as_object(cast(object, json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))))
    return str(claims.get("sub", ""))


def reply(status: int, body: object = None, **headers: str) -> O.Reply:
    text = body if isinstance(body, str) else ("" if body is None else json.dumps(body))
    return O.Reply(status, {k.lower().replace("_", "-"): v for k, v in headers.items()}, text)


class FakeShop:
    """Just enough of Shopware's OAuth and MCP endpoints for the flow."""

    def __init__(self, *, discovery: bool = True, registered: bool = True, gaps: bool = False) -> None:
        self.discovery: bool = discovery
        self.registered: bool = registered
        self.gaps: bool = gaps
        self.codes: dict[str, tuple[str, str, str]] = {}
        self.refresh: dict[str, tuple[str, str]] = {}
        self.requests: list[tuple[str, str]] = []
        # username -> (password, allowlist); None allowlist means unset.
        self.users: dict[str, tuple[str, tuple[str, ...] | None]] = {
            u.username: (u.password, u.tools) for u in (ALLOWLISTED, UNSET)
        }
        # The allowlist the server applies, by username; a leak widens it.
        self.leak: bool = False

    def allowlist(self, headers: Mapping[str, str]) -> set[str]:
        """What the principal behind a bearer token may reach; admin: everything."""
        user = subject(headers.get("Authorization", "").removeprefix("Bearer "))
        if user not in self.users or self.leak:
            return set(EVERYTHING)
        return set(META_TOOLS) | set(self.users[user][1] or ())

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
        if path == "/api/oauth/authorize/info":
            return reply(200, {"client": {"id": query.get("client_id")}, "resource": query.get("resource")})
        if path == "/api/oauth/authorize":
            bearer = headers.get("Authorization", "").removeprefix("Bearer ")
            if bearer.startswith("user-"):
                form["_consenting"] = bearer.removeprefix("user-")
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
        user = subject(auth.removeprefix("Bearer "))
        if rpc.get("method") == "tools/list" and user not in self.users:
            return reply(200, 'event: message\ndata: {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "a"}]}}')
        if rpc.get("method") == "tools/list":
            names = sorted(self.allowlist(headers))
            cursor = str(as_object(rpc.get("params")).get("cursor", ""))
            page, rest = (names[:2], names[2:]) if not cursor else (names[2:], [])
            result: JsonObject = {"tools": [{"name": n} for n in page]}
            if rest:
                result["nextCursor"] = "p2"
            return reply(200, {"jsonrpc": "2.0", "id": 2, "result": result})
        if rpc.get("method") == "tools/call":
            name = str(as_object(rpc.get("params")).get("name"))
            if name not in self.allowlist(headers):
                message = f'Tool "{name}" is not enabled in your MCP allowlist.'
                return reply(200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": message}})
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
        consenting = headers_user(form)
        self.codes[code] = (form.get("resource", ""), form["code_challenge"], consenting)
        return reply(
            200, {"redirectUri": f"{redirect}?{urlencode({'code': code, 'state': form['state'], 'iss': ISSUER})}"}
        )

    def token(self, form: Mapping[str, str]) -> O.Reply:
        grant = form["grant_type"]
        if grant == "client_credentials":
            return reply(200, {"access_token": "admin-token"})
        if grant == "password":
            known = self.users.get(form["username"])
            if known is None or known[0] != form["password"]:
                return reply(400, {"errors": [{"code": "FRAMEWORK__API_INVALID_CREDENTIALS"}]})
            return reply(200, {"access_token": f"user-{form['username']}"})
        if grant == "refresh_token":
            stored = self.refresh.get(form["refresh_token"])
            if stored is None or form.get("resource", stored[0]) != stored[0]:
                return reply(400, {"error": "invalid_grant"})
            return self.issue(form["client_id"], stored[0], stored[1])
        issued = self.codes.pop(form.get("code", ""), None)
        if issued is None:
            return reply(400, {"error": "invalid_grant"})
        resource, challenge, user = issued
        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
            return reply(400, {"error": "invalid_grant"})
        if form.get("resource") and not resource:
            hint = f"The authorization was {O.GRANTED_WITHOUT_RESOURCE}."
            return reply(400, {"error": "invalid_target", "hint": hint})
        if form.get("resource", resource) != resource:
            return reply(400, {"error": "invalid_target"})
        return self.issue(form["client_id"], resource, user)

    def issue(self, client: str, resource: str, user: str = "admin") -> O.Reply:
        refresh = f"rt-{len(self.refresh)}"
        self.refresh[refresh] = (resource, user)
        aud = [client, resource] if resource else [client]
        return reply(200, {"access_token": jwt(aud, user), "refresh_token": refresh})


def headers_user(form: Mapping[str, str]) -> str:
    """Who posted the consent decision: the fake reads it off the form, where
    `FakeShop.__call__` puts the bearer token's user."""
    return form.get("_consenting", "admin")


@contextmanager
def two_users() -> Generator[Sequence[SignInUser]]:
    yield (ALLOWLISTED, UNSET)


def run(
    shop: FakeShop,
    monkeypatch: pytest.MonkeyPatch,
    client: str = "",
    *,
    users: O.Users | None = None,
) -> Reporter:
    if client:
        monkeypatch.setenv(O.CLIENT_ENV, client)
    else:
        monkeypatch.delenv(O.CLIENT_ENV, raising=False)
    rep = Reporter(BASE, color=False)
    O.verify_oauth_flow(rep, ADMIN, send=shop, provision=users is not None, users=users)
    return rep


def failures(rep: Reporter) -> list[str]:
    return [r["label"] for r in rep.records if r["status"] == "fail"]


def test_a_shop_without_discovery_is_one_skip_naming_the_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(discovery=False), monkeypatch)

    assert (rep.passed, rep.failed, rep.skipped) == (0, 0, 1)
    [record] = rep.records
    assert record["label"] == O.FLOW_CHECK and record["status"] == "skipped"
    assert "shopware/shopware#21862" in record.get("reason", "")


def test_the_whole_flow_passes_on_the_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(), monkeypatch)

    assert failures(rep) == [] and rep.exit_code == 0
    assert rep.passed == 39 and rep.skipped == 1
    assert rep.records[-1].get("reason") == O.NEEDS_PROVISIONING


def test_the_first_drafts_gaps_are_new_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in the registry excuses them any more: a regression is reported as new."""
    rep = run(FakeShop(gaps=True), monkeypatch)

    assert failures(rep) == [
        "MCP-bound token refused on /api/_info/system-health-check",
        "MCP-bound token gets only the anonymous admin snippets",
    ]
    assert rep.unexpected_failed == 2 and rep.exit_code != 0
    errors = [str(r.get("error")) for r in rep.records if r["status"] == "fail"]
    assert errors == ["HTTP 200, expected 401 with the health report", "4 groups with the token, 2 without"]


def test_an_unregistered_client_skips_after_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(registered=False), monkeypatch, client="someone-else")

    skip = rep.records[-1]
    assert skip["status"] == "skipped" and "'someone-else'" in skip.get("reason", "")
    assert O.CONFIG_FILE in skip.get("reason", "")
    assert rep.failed == 0


def test_the_client_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(gaps=False), monkeypatch, client="claude-code")

    assert rep.failed == 0, failures(rep)


def test_consent_info_without_the_resource_fails_one_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server from before the consent page showed the resource."""
    shop = FakeShop()
    real = shop.__call__

    def old_info(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        if "/api/oauth/authorize/info" in url:
            return reply(200, {"client": {"id": "x"}})
        return real(method, url, headers, body)

    rep = run(cast(FakeShop, old_info), monkeypatch)

    assert failures(rep) == ["consent information names the MCP endpoint as the resource"]


def test_a_refusal_without_the_consent_page_hint_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare "does not match" sends people to the client instead of the Administration."""
    shop = FakeShop()
    real = shop.__call__

    def no_hint(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        answer = real(method, url, headers, body)
        if O.GRANTED_WITHOUT_RESOURCE in answer.text:
            return reply(400, {"error": "invalid_target"})
        return answer

    rep = run(cast(FakeShop, no_hint), monkeypatch)

    assert failures(rep) == ["consent without the resource: the token request is refused and names the consent page"]


def test_refusals_need_a_code_to_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """When consent yields no code, every token request fails, so a refusal proves nothing."""
    shop = FakeShop()
    real = shop.__call__
    calls = {"approve": 0}

    def codeless(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        if method == "POST" and url.endswith("/api/oauth/authorize"):
            calls["approve"] += 1
            if calls["approve"] > 1:
                return reply(500, "boom")
        return real(method, url, headers, body)

    rep = run(cast(FakeShop, codeless), monkeypatch)

    refusals = [
        "token request for a different resource refused",
        "wrong PKCE verifier refused",
        "consent without the resource: the token request is refused and names the consent page",
    ]
    for label in refusals:
        [record] = [r for r in rep.records if r["label"] == label]
        assert record["status"] == "fail" and "no code to exchange" in str(record.get("error"))


def test_a_refresh_that_breaks_does_not_pass_the_resource_check(monkeypatch: pytest.MonkeyPatch) -> None:
    shop = FakeShop()
    real = shop.__call__

    def broken_refresh(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        if b"grant_type=refresh_token" in (body or b""):
            return reply(500, "boom")
        return real(method, url, headers, body)

    rep = run(cast(FakeShop, broken_refresh), monkeypatch)

    [record] = [r for r in rep.records if r["label"] == "refresh for a different resource refused"]
    assert record["status"] == "fail" and record.get("error") == "no refresh token to try it with"


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

    def without_refresh(client: str, resource: str, user: str = "admin") -> O.Reply:
        issued = real(client, resource, user).json()
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


# --- a non-admin user -------------------------------------------------------------


def restricted(rep: Reporter) -> dict[str, str]:
    return {
        str(r["label"]): str(r["status"])
        for r in rep.records
        if str(r["label"]).startswith(("allowlisted user", "user without", O.RESTRICTED_CHECK))
    }


def test_the_allowlist_holds_for_a_users_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(gaps=False), monkeypatch, users=two_users)

    assert rep.failed == 0, failures(rep)
    assert restricted(rep) == {
        "allowlisted user: signs in to the Administration": "pass",
        "allowlisted user: consents and gets an MCP-bound token": "pass",
        "allowlisted user: opens an MCP session": "pass",
        "allowlisted user: tools/list with ?toolsets=all shows only its allowlist and the meta-tools": "pass",
        "allowlisted user: calls shopware-entity-search": "pass",
        f"allowlisted user: is refused {O.REFUSED_TOOL}, which is not allowlisted": "pass",
        "user without an allowlist: signs in to the Administration": "pass",
        "user without an allowlist: consents and gets an MCP-bound token": "pass",
        "user without an allowlist: opens an MCP session": "pass",
        "user without an allowlist: tools/list with ?toolsets=all shows only its allowlist and the meta-tools": "pass",
        "user without an allowlist: is refused shopware-entity-search, which is not allowlisted": "pass",
    }


def test_the_lists_are_read_with_every_toolset_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    shop = FakeShop(gaps=False)
    real = shop.__call__

    def recording(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        if subject(headers.get("Authorization", "").removeprefix("Bearer ")) == ALLOWLISTED.username:
            seen.append(url)
        return real(method, url, headers, body)

    rep = run(cast(FakeShop, recording), monkeypatch, users=two_users)

    assert rep.failed == 0, failures(rep)
    assert seen and all(url == f"{MCP}?toolsets=all" for url in seen)


def test_a_server_ignoring_the_allowlist_fails_both_users(monkeypatch: pytest.MonkeyPatch) -> None:
    shop = FakeShop(gaps=False)
    shop.leak = True
    rep = run(shop, monkeypatch, users=two_users)

    assert failures(rep) == [
        "allowlisted user: tools/list with ?toolsets=all shows only its allowlist and the meta-tools",
        f"allowlisted user: is refused {O.REFUSED_TOOL}, which is not allowlisted",
        "user without an allowlist: tools/list with ?toolsets=all shows only its allowlist and the meta-tools",
        "user without an allowlist: is refused shopware-entity-search, which is not allowlisted",
    ]


def test_a_user_who_cannot_sign_in_stops_there(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def wrong_password() -> Generator[Sequence[SignInUser]]:
        yield (SignInUser("x", ALLOWLISTED.username, "wrong", OAUTH_TOOLS),)

    rep = run(FakeShop(gaps=False), monkeypatch, users=wrong_password)

    assert restricted(rep) == {"allowlisted user: signs in to the Administration": "fail"}


def test_a_refused_code_exchange_for_the_user_stops_there(monkeypatch: pytest.MonkeyPatch) -> None:
    shop = FakeShop(gaps=False)
    real = shop.__call__

    def refusing(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        if method == "POST" and url.endswith("/api/oauth/authorize") and "user-" in headers.get("Authorization", ""):
            return reply(403, {"errors": []})
        return real(method, url, headers, body)

    rep = run(cast(FakeShop, refusing), monkeypatch, users=two_users)

    assert restricted(rep)["allowlisted user: consents and gets an MCP-bound token"] == "fail"
    assert "allowlisted user: opens an MCP session" not in restricted(rep)


def test_a_session_the_user_cannot_open_stops_there(monkeypatch: pytest.MonkeyPatch) -> None:
    shop = FakeShop(gaps=False)
    real = shop.__call__

    def no_session(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> O.Reply:
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        if url.startswith(MCP + "?") and subject(token) in shop.users:
            return reply(500, "boom")
        return real(method, url, headers, body)

    rep = run(cast(FakeShop, no_session), monkeypatch, users=two_users)

    assert restricted(rep)["allowlisted user: opens an MCP session"] == "fail"
    assert "allowlisted user: calls shopware-entity-search" not in restricted(rep)


class Refused(AbstractContextManager[Sequence[SignInUser]]):
    """Users the Admin API refused to create."""

    @override
    def __enter__(self) -> Sequence[SignInUser]:  # pyright: ignore[reportMissingSuperCall] -- refuses, as the API did
        raise ProvisioningFailed("POST /api/user answered HTTP 400")

    @override
    def __exit__(self, *_exc: object) -> None:
        return None


def test_users_that_cannot_be_created_fail_one_check(monkeypatch: pytest.MonkeyPatch) -> None:
    rep = run(FakeShop(gaps=False), monkeypatch, users=Refused)

    assert restricted(rep) == {O.RESTRICTED_CHECK: "fail"}
    assert "HTTP 400" in str(rep.records[-1].get("error"))


def test_provisioning_builds_the_users_from_the_suites_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple[str, str, str]] = []

    def admin_api(base: str, key: str, secret: str) -> object:
        made.append((base, key, secret))
        return object()

    monkeypatch.setattr(O, "AdminApi", admin_api)

    def sign_in_users(_api: object) -> AbstractContextManager[Sequence[SignInUser]]:
        return two_users()

    monkeypatch.setattr(O, "sign_in_users", sign_in_users)

    with O.provisioned_users(ADMIN)() as users:
        assert list(users) == [ALLOWLISTED, UNSET]
    assert made == [(BASE, "SWUAadmin", "secret")]
