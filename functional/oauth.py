"""The MCP OAuth flow, end to end, with no browser and no model.

shopware/shopware#21862 lets an MCP client sign in to `/api/_mcp` the way the MCP
authorization spec describes: a 401 whose challenge names the protected resource
metadata, that document naming the authorization server, the authorization code
grant with PKCE for a public client, and an access token bound to the MCP
endpoint by its audience (RFC 8707), refused everywhere else in the Admin API.

This module walks that flow the way a client does, and stands in for the human
at the one step a client cannot do: the consent decision is posted with the
suite's own administrator token, which is what the Administration's consent
page sends. Everything else is the client's side of the protocol.

Three things it does not do:

  * fail on a shop without discovery. A Shopware from before #21862 answers the
    401 without `resource_metadata` and serves no metadata document; that is one
    skip naming the PR, not 34 failures.
  * register its own client. Public clients are configuration
    (`shopware.api.oauth_clients`), and setup-lane writes `mcp-evals-probe` into
    `config/packages/zzz-mcp-evals-oauth.yaml`. A shop without it is a skip that
    says so. `MCP_EVALS_OAUTH_CLIENT` names another one.
  * clean up. Each run leaves a few authorization codes and refresh tokens for the
    administrator and the probe client, which is what any sign-in leaves.

Two endpoints check a bearer token themselves rather than through the Admin API
firewall: the system health check and the Administration snippets. Both are
asked whether they refuse an MCP-bound token, because the first draft of #21862
let it through on both.

With `--provision-principals` it signs in four more times, as users it creates
through the Admin API and deletes after (functional/principals.py): non-admin
users with no allowlist, one tool and "All capabilities", and an administrator
with one tool saved. The allowlist applies to a bearer token like to an access
key (`McpAllowlistProvider::forCurrentRequest()`), so each token goes through the
allowlist matrix's own checks, on all eight surfaces, with the same
expectations as a key: functional/runner.py `verify_principal`.

Two more things a client depends on:

  * `?toolsets=` on the MCP URL (shopware/shopware#20509) survives discovery. The
    challenge and the metadata both name the pinned URL, and a grant for it
    yields a token for the bare endpoint that opens a session on both.
  * redirect URIs. An https one and a native app's private-use scheme
    (`cursor://`) go through consent. Plain http outside loopback is refused
    with `FRAMEWORK__OAUTH_INSECURE_REDIRECT_URI` before anything is sent to it,
    except in the `dev` environment, which allows it for every host. Which
    environment the lane runs in comes from `MCP_EVALS_APP_ENV`, which setup-lane
    exports; without it that one case is a skip.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

from eval.result_schema import JsonObject, as_list, as_object
from functional.principals import AdminApi, Principal, ProvisioningFailed, SignInUser, sign_in_users
from functional.reporting import Reporter
from mcp_client import ALL_TOOLSETS, DEFAULT_PROTOCOL_VERSION, Endpoint

FIX = "shopware/shopware#21862"
FIX_URL = "https://github.com/shopware/shopware/pull/21862"

CLIENT_ENV = "MCP_EVALS_OAUTH_CLIENT"
DEFAULT_CLIENT = "mcp-evals-probe"
CONFIG_FILE = "config/packages/zzz-mcp-evals-oauth.yaml"
# The client is registered with `http://127.0.0.1/callback`. A loopback IP
# literal matches on any port (RFC 8252 7.3), so the port here is arbitrary and
# nothing has to listen on it: the code is read off the consent decision.
REDIRECT = "http://127.0.0.1:41234/callback"
TIMEOUT_S = 30

FLOW_CHECK = "MCP OAuth flow"
NO_DISCOVERY = (
    "the server has no OAuth discovery for {mcp}: the 401 challenge names no resource_metadata and "
    f"the protected resource metadata is not served. It predates {FIX} ({FIX_URL})"
)
UNREGISTERED = (
    "the public OAuth client {client!r} is not registered on this shop. setup-lane writes it to "
    f"{CONFIG_FILE} under shopware.api.oauth_clients; set {CLIENT_ENV} to use another client"
)

# What ShopwareAuthCodeGrantType says when the consent decision dropped the
# resource the authorization request named: an Administration built before
# resource support. A bare "does not match" sent people looking at the client.
GRANTED_WITHOUT_RESOURCE = "granted without a resource"

ACCEPT_MCP = {"Accept": "application/json, text/event-stream"}

RESTRICTED_CHECK = "OAuth as other users"
NEEDS_PROVISIONING = (
    "needs --provision-principals: it creates users with each allowlist state and signs in as each of them"
)

# `?toolsets=` on the URL a client is given (shopware/shopware#20509).
PINNED_QUERIES = (f"?toolsets={ALL_TOOLSETS}", "?toolsets=order,media")

# A second public client, registered by setup-lane with one redirect URI of each
# kind. Nothing listens on any of them: the code is read off the consent decision.
REDIRECT_CLIENT_ENV = "MCP_EVALS_OAUTH_REDIRECT_CLIENT"
DEFAULT_REDIRECT_CLIENT = "mcp-evals-redirects"
HTTPS_REDIRECT = "https://mcp-evals.example/callback"
PRIVATE_USE_REDIRECT = "cursor://mcp-evals/callback"
PLAIN_HTTP_REDIRECT = "http://mcp-evals.example/callback"
INSECURE_REDIRECT = "FRAMEWORK__OAUTH_INSECURE_REDIRECT_URI"
APP_ENV_VAR = "MCP_EVALS_APP_ENV"
REDIRECT_CHECK = "OAuth redirect URIs"
UNREGISTERED_REDIRECTS = (
    "the public OAuth client {client!r} is not registered on this shop. setup-lane writes it to "
    f"{CONFIG_FILE} with one https, one cursor:// and one plain-http redirect URI; set {REDIRECT_CLIENT_ENV} "
    "to use another client"
)
UNKNOWN_APP_ENV = (
    f"{APP_ENV_VAR} is not set, and plain http outside loopback is refused except under APP_ENV=dev, "
    "so the expected answer is unknown"
)

Users = Callable[[], AbstractContextManager[Sequence[SignInUser]]]
Verify = Callable[[Principal], None]


@dataclass(frozen=True)
class Reply:
    """One HTTP answer. Header names are lower-cased so a lookup cannot miss on case."""

    status: int
    headers: Mapping[str, str]
    text: str

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    def json(self) -> JsonObject:
        try:
            return as_object(cast(object, json.loads(self.text)))
        except ValueError:
            return {}


Send = Callable[[str, str, Mapping[str, str], bytes | None], Reply]


def send_http(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Reply:
    """The real transport. Redirects are not followed: the Location IS the answer."""
    resp = requests.request(method, url, headers=dict(headers), data=body, allow_redirects=False, timeout=TIMEOUT_S)
    return Reply(resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.text)


def pkce() -> tuple[str, str]:
    """A verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def jwt_audience(token: str) -> list[str]:
    """The `aud` claim of a JWT as a list, or [] for anything that is not one."""
    parts = token.split(".")
    if len(parts) != 3:
        return []
    try:
        claims = as_object(cast(object, json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))))
    except ValueError:
        return []
    aud = claims.get("aud")
    if isinstance(aud, str):
        return [aud] if aud else []
    return [str(a) for a in as_list(aud)]


def resource_metadata_url(challenge: str) -> str:
    """The `resource_metadata` parameter of a Bearer challenge, or ''."""
    marker = 'resource_metadata="'
    if marker not in challenge:
        return ""
    return challenge.split(marker, 1)[1].split('"', 1)[0]


def well_known_metadata_url(mcp_url: str) -> str:
    """Where RFC 9728 puts the metadata when no challenge names it."""
    parts = urlsplit(mcp_url)
    return f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{parts.path}"


def authorization_server_metadata_url(issuer: str) -> str:
    """RFC 8414's path insertion: the well-known segment goes before the issuer's path."""
    parts = urlsplit(issuer)
    return f"{parts.scheme}://{parts.netloc}/.well-known/oauth-authorization-server{parts.path}"


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


def mcp_body(text: str) -> JsonObject:
    """A JSON-RPC reply, whether it came as JSON or as an SSE stream."""
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    try:
        return as_object(cast(object, json.loads(data[-1] if data else text)))
    except ValueError:
        return {}


@dataclass
class Grant:
    """What an authorization request came back with."""

    status: int
    location: str
    redirect: str
    verifier: str
    state: str

    @property
    def params(self) -> dict[str, str]:
        return query_of(self.redirect)


class OAuthFlow:
    """One walk through the flow against one shop."""

    def __init__(
        self,
        rep: Reporter,
        endpoint: Endpoint,
        client: str,
        send: Send,
        users: Users | None = None,
        verify: Verify | None = None,
    ):
        self.rep: Reporter = rep
        self.users: Users | None = users
        self.verify: Verify | None = verify
        self.base: str = endpoint.base_url
        self.mcp_url: str = f"{endpoint.base_url}{endpoint.path}"
        self.access_key: str = endpoint.auth_headers.get("sw-access-key", "")
        self.secret_key: str = endpoint.auth_headers.get("sw-secret-access-key", "")
        self.client: str = client
        self.send: Send = send
        self.admin_token: str = ""
        self.issuer: str = ""

    # -- transport ----------------------------------------------------------
    def http(
        self,
        method: str,
        url: str,
        *,
        json_body: object = None,
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        h = {"Accept": "application/json", **(headers or {})}
        body: bytes | None = None
        if form is not None:
            body = urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        return self.send(method, url if url.startswith("http") else self.base + url, h, body)

    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def mcp(
        self, payload: JsonObject, *, token: str = "", session: str = "", query: str = ""
    ) -> tuple[Reply, JsonObject]:
        headers = {**ACCEPT_MCP, "MCP-Protocol-Version": DEFAULT_PROTOCOL_VERSION}
        if token:
            headers.update(self.bearer(token))
        if session:
            headers["Mcp-Session-Id"] = session
        reply = self.http("POST", self.mcp_url + query, json_body=payload, headers=headers)
        return reply, mcp_body(reply.text)

    def initialize(self, token: str, query: str = "") -> tuple[Reply, str]:
        reply, _ = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-evals-oauth", "version": "0"},
                },
            },
            token=token,
            query=query,
        )
        session = reply.header("Mcp-Session-Id")
        if reply.status == 200 and session:
            _ = self.mcp(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}, token=token, session=session, query=query
            )
        return reply, session

    # -- reporting ----------------------------------------------------------
    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.rep.check_pass(label)
        else:
            self.rep.check_fail(label, detail or "no detail")
        return ok

    # -- the grant ----------------------------------------------------------
    def authorize(
        self,
        *,
        resource: str | None = "",
        approved: bool = True,
        redirect: str = REDIRECT,
        decide: bool = True,
        consent_token: str = "",
        forward_resource: bool = True,
        client: str = "",
    ) -> Grant:
        """GET authorize as the client, then post the consent decision as the
        admin, or as whoever `consent_token` belongs to.

        `resource=""` means the MCP endpoint, None leaves the parameter out.
        `forward_resource=False` posts the decision without it, as a consent
        page built before resource support does.
        """
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(8)
        query = {
            "response_type": "code",
            "client_id": client or self.client,
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "write",
        }
        if resource is not None:
            query["resource"] = resource or self.mcp_url
        reply = self.http("GET", "/api/oauth/authorize?" + urlencode(query))
        location = reply.header("Location") or reply.text[:300]
        if not decide or reply.status != 302 or "error=" in location:
            return Grant(reply.status, location, "", verifier, state)
        decided = {k: v for k, v in query.items() if forward_resource or k != "resource"}
        decision = self.http(
            "POST",
            "/api/oauth/authorize",
            json_body={**decided, "approved": approved},
            headers=self.bearer(consent_token or self.admin_token),
        )
        redirect_uri = str(decision.json().get("redirectUri", ""))
        return Grant(reply.status, location, redirect_uri or decision.text[:300], verifier, state)

    def token(self, grant: Grant, *, resource: str | None = "", verifier: str = "") -> Reply:
        form = {
            "grant_type": "authorization_code",
            "client_id": self.client,
            "redirect_uri": REDIRECT,
            "code": grant.params.get("code", ""),
            "code_verifier": verifier or grant.verifier,
        }
        if resource is not None:
            form["resource"] = resource or self.mcp_url
        return self.http("POST", "/api/oauth/token", form=form)

    # -- the walk -----------------------------------------------------------
    def run(self) -> None:
        if not self.discovery():
            return
        if not self.admin_sign_in():
            return
        grant = self.authorize()
        if grant.status == 403 or "INVALID_ACCESS_KEY" in grant.location:
            self.rep.check_skip(FLOW_CHECK, UNREGISTERED.format(client=self.client))
            return
        token = self.happy_path(grant)
        if not token:
            return
        self.bound_token(str(token["access_token"]))
        self.refresh(str(token.get("refresh_token", "")))
        self.negatives()
        self.unbound()
        self.pinned_resource()
        self.redirects()
        self.restricted()

    def discovery(self) -> bool:
        """The 401 challenge and both metadata documents. False: nothing to test."""
        init: JsonObject = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        anonymous = self.http("POST", self.mcp_url, json_body=init, headers=ACCEPT_MCP)
        challenge = anonymous.header("WWW-Authenticate")
        prm_url = resource_metadata_url(challenge) or well_known_metadata_url(self.mcp_url)
        prm_reply = self.http("GET", prm_url)
        prm = prm_reply.json()
        if not resource_metadata_url(challenge) and (prm_reply.status != 200 or not prm):
            self.rep.check_skip(FLOW_CHECK, NO_DISCOVERY.format(mcp=self.mcp_url))
            return False

        self.check("401 without credentials", anonymous.status == 401, f"HTTP {anonymous.status}")
        self.check(
            "challenge names resource_metadata and no error code",
            bool(resource_metadata_url(challenge)) and "error=" not in challenge,
            challenge or "no WWW-Authenticate header",
        )
        bad_bearer = self.http("POST", self.mcp_url, json_body=init, headers={**ACCEPT_MCP, **self.bearer("garbage")})
        self.check(
            "bad bearer token: 401 with error=invalid_token",
            bad_bearer.status == 401 and 'error="invalid_token"' in bad_bearer.header("WWW-Authenticate"),
            f"HTTP {bad_bearer.status} {bad_bearer.header('WWW-Authenticate')}",
        )
        bad_key = self.http(
            "POST",
            self.mcp_url,
            json_body=init,
            headers={**ACCEPT_MCP, "sw-access-key": "SWUAnope", "sw-secret-access-key": "nope"},
        )
        self.check(
            "bad access key: 401 with error=invalid_token",
            bad_key.status == 401 and 'error="invalid_token"' in bad_key.header("WWW-Authenticate"),
            f"HTTP {bad_key.status} {bad_key.header('WWW-Authenticate')}",
        )
        store = self.http("POST", "/store-api/_mcp", json_body=init, headers=ACCEPT_MCP)
        self.check(
            "Store API 401 carries no OAuth challenge",
            "resource_metadata" not in store.header("WWW-Authenticate"),
            f"HTTP {store.status} {store.header('WWW-Authenticate')}",
        )

        self.check("protected resource metadata served", prm_reply.status == 200, f"HTTP {prm_reply.status} {prm_url}")
        self.check(
            "metadata resource is the MCP endpoint", prm.get("resource") == self.mcp_url, str(prm.get("resource"))
        )
        servers = as_list(prm.get("authorization_servers"))
        self.issuer = str(servers[0]) if servers else ""
        asm_url = authorization_server_metadata_url(self.issuer)
        asm_reply = self.http("GET", asm_url)
        asm = asm_reply.json()
        self.check(
            "authorization server metadata at the RFC 8414 URL",
            bool(self.issuer) and asm_reply.status == 200,
            f"HTTP {asm_reply.status} {asm_url}",
        )
        self.check(
            "issuer matches the protected resource metadata",
            bool(self.issuer) and asm.get("issuer") == self.issuer,
            f"{asm.get('issuer')} != {self.issuer}",
        )
        methods = [str(m) for m in as_list(asm.get("code_challenge_methods_supported"))]
        self.check("S256 PKCE advertised", "S256" in methods, str(methods))
        self.check(
            "no registration_endpoint (no dynamic client registration)",
            "registration_endpoint" not in asm,
            str(asm.get("registration_endpoint")),
        )
        return True

    def admin_sign_in(self) -> bool:
        reply = self.http(
            "POST",
            "/api/oauth/token",
            json_body={
                "grant_type": "client_credentials",
                "client_id": self.access_key,
                "client_secret": self.secret_key,
            },
        )
        self.admin_token = str(reply.json().get("access_token", ""))
        return self.check(
            "administrator token for the consent decision", bool(self.admin_token), f"HTTP {reply.status}"
        )

    def happy_path(self, grant: Grant) -> JsonObject:
        """Consent, code, token, and the token on the MCP endpoint. {} when it broke."""
        self.check(
            "authorize redirects to the Administration consent page",
            grant.status == 302 and "/admin" in grant.location,
            f"HTTP {grant.status} {grant.location[:140]}",
        )
        self.check("consent page receives the resource", "resource=" in grant.location, grant.location[:140])
        # What the consent page asks before it renders: the resource is what
        # lets it say the access is limited to the MCP endpoint.
        consent_query = grant.location.split("?", 1)[1] if "?" in grant.location else ""
        info = self.http("GET", "/api/oauth/authorize/info?" + consent_query, headers=self.bearer(self.admin_token))
        self.check(
            "consent information names the MCP endpoint as the resource",
            info.status == 200 and info.json().get("resource") == self.mcp_url,
            f"HTTP {info.status} resource={info.json().get('resource')!r}",
        )
        params = grant.params
        self.check(
            "approval redirect carries the code and the state",
            "code" in params and params.get("state") == grant.state,
            grant.redirect[:140],
        )
        self.check(
            "approval redirect carries iss (RFC 9207)",
            bool(self.issuer) and params.get("iss") == self.issuer,
            str(params.get("iss")),
        )
        reply = self.token(grant)
        token = reply.json()
        access = str(token.get("access_token", ""))
        if not self.check("code, PKCE and resource exchanged for a token", bool(access), f"HTTP {reply.status}"):
            return {}
        aud = jwt_audience(access)
        self.check(
            "token audience is the client and the MCP endpoint",
            aud[:1] == [self.client] and self.mcp_url in aud,
            json.dumps(aud),
        )

        init, session = self.initialize(access)
        self.check("bearer token opens an MCP session", init.status == 200 and bool(session), f"HTTP {init.status}")
        listed, body = self.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token=access, session=session)
        names = [str(as_object(t).get("name")) for t in as_list(as_object(body.get("result")).get("tools"))]
        self.check("tools/list over OAuth", listed.status == 200 and bool(names), f"HTTP {listed.status}")
        called, body = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "shopware-toolsets-list", "arguments": {}},
            },
            token=access,
            session=session,
        )
        result = as_object(body.get("result"))
        self.check(
            "tools/call over OAuth",
            called.status == 200 and bool(result) and result.get("isError") is not True,
            f"HTTP {called.status} {called.text[:140]}",
        )
        if session:
            _ = self.http("DELETE", self.mcp_url, headers={**self.bearer(access), "Mcp-Session-Id": session})
        return token

    def bound_token(self, access: str) -> None:
        """An MCP-bound token is for /api/_mcp and nothing else in the Admin API."""
        for method, path in (("GET", "/api/_info/me"), ("GET", "/api/_info/version"), ("POST", "/api/search/product")):
            reply = self.http(method, path, json_body={} if method == "POST" else None, headers=self.bearer(access))
            self.check(f"MCP-bound token refused on {path}", reply.status == 401, f"HTTP {reply.status}")

        health = self.http("GET", "/api/_info/system-health-check", headers=self.bearer(access))
        self.check(
            "MCP-bound token refused on /api/_info/system-health-check",
            health.status == 401,
            f"HTTP {health.status}, expected 401" + (" with the health report" if health.status == 200 else ""),
        )

        # The endpoint is public: anonymously it answers the login page's
        # snippets. A token that may not use the Admin API must not get more.
        path = "/api/_admin/snippets?locale=en-GB"
        anonymous = snippet_groups(self.http("GET", path))
        bound = snippet_groups(self.http("GET", path, headers=self.bearer(access)))
        self.check(
            "MCP-bound token gets only the anonymous admin snippets",
            bool(anonymous) and bound == anonymous,
            f"{len(bound)} groups with the token, {len(anonymous)} without",
        )

    def refresh(self, refresh_token: str) -> None:
        if not self.check("refresh token issued", bool(refresh_token), "none in the token response"):
            return
        reply = self.http(
            "POST",
            "/api/oauth/token",
            form={"grant_type": "refresh_token", "client_id": self.client, "refresh_token": refresh_token},
        )
        renewed = reply.json()
        access = str(renewed.get("access_token", ""))
        aud = jwt_audience(access)
        self.check(
            "refreshed token still bound to the MCP endpoint",
            reply.status == 200 and self.mcp_url in aud,
            f"HTTP {reply.status} {aud}",
        )
        me = self.http("GET", "/api/_info/me", headers=self.bearer(access))
        self.check("refreshed token still refused on the Admin API", me.status == 401, f"HTTP {me.status}")
        renewed_refresh = str(renewed.get("refresh_token", ""))
        if not renewed_refresh:
            # A refusal of an empty token proves nothing about the resource.
            self.rep.check_fail("refresh for a different resource refused", "no refresh token to try it with")
            return
        other = self.http(
            "POST",
            "/api/oauth/token",
            form={
                "grant_type": "refresh_token",
                "client_id": self.client,
                "refresh_token": renewed_refresh,
                "resource": f"{self.base}/api/other",
            },
        )
        self.check("refresh for a different resource refused", other.status >= 400, f"HTTP {other.status}")

    def refused(self, label: str, grant: Grant, reply: Reply, *, says: str = "") -> None:
        """A token request that must fail, for the reason under test only: with
        no code to exchange, any refusal would pass."""
        if "code" not in grant.params:
            self.rep.check_fail(label, f"no code to exchange: {grant.redirect[:120] or grant.location[:120]}")
            return
        self.check(
            label,
            reply.status >= 400 and says in reply.text,
            f"HTTP {reply.status} {reply.text[:160]}",
        )

    def negatives(self) -> None:
        foreign = self.authorize(resource="https://evil.example/api/_mcp")
        self.check(
            "foreign resource answered with invalid_target",
            "invalid_target" in foreign.location + foreign.redirect,
            (foreign.location or foreign.redirect)[:160],
        )
        grant = self.authorize()
        self.refused(
            "token request for a different resource refused",
            grant,
            self.token(grant, resource=f"{self.base}/api/other"),
        )
        grant = self.authorize()
        self.refused("wrong PKCE verifier refused", grant, self.token(grant, verifier="wrong-verifier-" + "x" * 40))
        grant = self.authorize(forward_resource=False)
        self.refused(
            "consent without the resource: the token request is refused and names the consent page",
            grant,
            self.token(grant),
            says=GRANTED_WITHOUT_RESOURCE,
        )
        denied = self.authorize(approved=False)
        self.check(
            "denied consent redirects with access_denied and iss",
            "error=access_denied" in denied.redirect and "iss=" in denied.redirect,
            denied.redirect[:140],
        )
        localhost = self.authorize(redirect="http://localhost:41234/callback", decide=False)
        self.check(
            "localhost callback refused for a client registered on 127.0.0.1",
            localhost.status != 302 or "/admin" not in localhost.location,
            f"HTTP {localhost.status} {localhost.location[:120]}",
        )
        any_port = self.authorize(redirect="http://127.0.0.1:59999/callback", decide=False)
        self.check(
            "127.0.0.1 callback accepted on any port",
            any_port.status == 302 and "/admin" in any_port.location,
            f"HTTP {any_port.status} {any_port.location[:120]}",
        )

    def unbound(self) -> None:
        """Without a resource the code flow is what it was before #21862."""
        reply = self.token(self.authorize(resource=None), resource=None)
        access = str(reply.json().get("access_token", ""))
        me = self.http("GET", "/api/_info/me", headers=self.bearer(access)) if access else None
        self.check(
            "unbound code-flow token still works on the Admin API",
            reply.status == 200 and me is not None and me.status == 200,
            f"token HTTP {reply.status}, /api/_info/me HTTP {me.status if me else '-'}",
        )

    # -- ?toolsets= through discovery ----------------------------------------
    def pinned_resource(self) -> None:
        """A client given `/api/_mcp?toolsets=…` discovers, signs in and connects on it."""
        for query in PINNED_QUERIES:
            url = self.mcp_url + query
            init: JsonObject = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            challenge = self.http("POST", url, json_body=init, headers=ACCEPT_MCP).header("WWW-Authenticate")
            metadata_url = resource_metadata_url(challenge)
            self.check(
                f"{query}: the challenge's resource metadata URL keeps the query",
                query_of(metadata_url) == query_of(url),
                metadata_url or challenge or "no WWW-Authenticate header",
            )
            resource = self.http("GET", metadata_url).json().get("resource") if metadata_url else None
            self.check(f"{query}: the metadata resource keeps the query", resource == url, str(resource))

        url = self.mcp_url + PINNED_QUERIES[0]
        grant = self.authorize(resource=url)
        reply = self.token(grant, resource=url)
        access = str(reply.json().get("access_token", ""))
        aud = jwt_audience(access)
        if not self.check(
            "a grant for the pinned URL yields a token for the bare endpoint",
            reply.status == 200 and aud[:1] == [self.client] and self.mcp_url in aud and url not in aud,
            f"HTTP {reply.status} aud={json.dumps(aud)} {reply.text[:120] if reply.status != 200 else ''}",
        ):
            return
        for where, query in (("bare", ""), ("pinned", PINNED_QUERIES[0])):
            opened, session = self.initialize(access, query)
            self.check(
                f"that token opens a session on the {where} URL",
                opened.status == 200 and bool(session),
                f"HTTP {opened.status}",
            )
            if session:
                _ = self.http(
                    "DELETE", self.mcp_url + query, headers={**self.bearer(access), "Mcp-Session-Id": session}
                )

    # -- redirect URIs --------------------------------------------------------
    def redirects(self) -> None:
        """https and a private-use scheme go through; plain http outside loopback
        only in dev. One authorize per URI, on a client registered with all three."""
        client = os.environ.get(REDIRECT_CLIENT_ENV, "").strip() or DEFAULT_REDIRECT_CLIENT
        for kind, uri in (("https", HTTPS_REDIRECT), ("private-use (cursor://)", PRIVATE_USE_REDIRECT)):
            grant = self.authorize(redirect=uri, client=client, resource=None)
            if grant.status == 403 or "INVALID_ACCESS_KEY" in grant.location:
                self.rep.check_skip(REDIRECT_CHECK, UNREGISTERED_REDIRECTS.format(client=client))
                return
            self.check(
                f"{kind} redirect URI goes through consent and gets a code",
                grant.redirect.startswith(uri) and "code" in grant.params,
                f"HTTP {grant.status} {(grant.redirect or grant.location)[:140]}",
            )

        app_env = os.environ.get(APP_ENV_VAR, "").strip()
        label = "plain-http redirect URI outside loopback"
        if not app_env:
            self.rep.check_skip(label, UNKNOWN_APP_ENV)
            return
        plain = self.authorize(redirect=PLAIN_HTTP_REDIRECT, client=client, resource=None, decide=False)
        if app_env == "dev":
            self.check(
                f"{label} is accepted under APP_ENV=dev",
                plain.status == 302 and "/admin" in plain.location,
                f"HTTP {plain.status} {plain.location[:140]}",
            )
        else:
            self.check(
                f"{label} is refused with {INSECURE_REDIRECT} under APP_ENV={app_env}, and nothing is sent to it",
                plain.status == 400 and INSECURE_REDIRECT in plain.location,
                f"HTTP {plain.status} {plain.location[:140]}",
            )

    # -- other users --------------------------------------------------------
    def restricted(self) -> None:
        """Sign in as each provisioned user and run the allowlist matrix on its token."""
        if self.users is None or self.verify is None:
            self.rep.check_skip(RESTRICTED_CHECK, NEEDS_PROVISIONING)
            return
        try:
            with self.users() as users:
                for user in users:
                    self.restricted_user(user, self.verify)
        except ProvisioningFailed as exc:
            self.rep.check_fail(RESTRICTED_CHECK, f"could not provision the users: {exc}")

    def restricted_user(self, user: SignInUser, verify: Verify) -> None:
        who = user.label.split("-", 3)[-1]
        login = self.http(
            "POST",
            "/api/oauth/token",
            json_body={
                "grant_type": "password",
                "client_id": "administration",
                "scope": "write",
                "username": user.username,
                "password": user.password,
            },
        )
        consent = str(login.json().get("access_token", ""))
        if not self.check(f"{who}: signs in to the Administration", bool(consent), f"HTTP {login.status}"):
            return
        reply = self.token(self.authorize(consent_token=consent))
        access = str(reply.json().get("access_token", ""))
        if not self.check(
            f"{who}: consents and gets an MCP-bound token",
            bool(access) and self.mcp_url in jwt_audience(access),
            f"HTTP {reply.status} aud={json.dumps(jwt_audience(access))}",
        ):
            return
        verify(user.principal(access))


def snippet_groups(reply: Reply) -> set[str]:
    """The top-level snippet groups of every locale in an admin snippets answer."""
    return {group for locale in reply.json().values() for group in as_object(locale)}


def provisioned_users(endpoint: Endpoint) -> Users:
    """The two sign-in users, created with the suite's own credential."""

    def users() -> AbstractContextManager[Sequence[SignInUser]]:
        api = AdminApi(
            endpoint.base_url,
            endpoint.auth_headers.get("sw-access-key", ""),
            endpoint.auth_headers.get("sw-secret-access-key", ""),
        )
        return sign_in_users(api)

    return users


def verify_oauth_flow(
    rep: Reporter,
    endpoint: Endpoint,
    send: Send = send_http,
    *,
    provision: bool = False,
    users: Users | None = None,
    verify: Verify | None = None,
) -> None:
    """Walk the MCP OAuth flow; a skip naming #21862 on a shop without it.

    `provision` adds the other users; `users` replaces how they are made, and
    `verify` is what each user's token is then held to."""
    rep.section(f"MCP OAuth ({FIX})")
    client = os.environ.get(CLIENT_ENV, "").strip() or DEFAULT_CLIENT
    if provision and users is None:
        users = provisioned_users(endpoint)
    OAuthFlow(rep, endpoint, client, send, users if provision else None, verify).run()
