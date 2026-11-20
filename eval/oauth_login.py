"""Sign in to the Admin MCP endpoint over OAuth, as an MCP client does, instead of with an access key.

`functional/oauth.py` checks the OAuth flow itself, refusal by refusal. This
module only signs in, so that the LLM eval can run a whole fixture set on the
token a real client would hold (`eval.runner --auth oauth`). Everything it needs
it discovers, the way the MCP authorization spec tells a client to:

  1. an anonymous `initialize` on the MCP endpoint answers 401, and the
     `WWW-Authenticate` challenge names the protected resource metadata
     (RFC 9728). Without the parameter, the well-known URL is tried, as the spec
     allows;
  2. that document names the resource and the authorization server;
  3. the authorization server metadata (RFC 8414) names the authorization and
     token endpoints and the PKCE methods.

Then the authorization code grant with PKCE (S256) and the `resource`
parameter (RFC 8707) on both the authorization and the token request, so the
token is bound to the MCP endpoint by its audience. No URL of the grant is
hard-coded; only the client is configuration, as it is for any MCP client.

One step stands in for a human: the consent decision. It is posted with a token
for the principal behind the suite's own access key, which is what the
Administration's consent page sends for whoever is logged in. The OAuth token
therefore belongs to the same user as the key, and the two arms differ in how
they authenticate and in nothing else.

The access token lives ten minutes on a default Shopware, and an eval run lasts
longer, so `BearerToken` refreshes it shortly before it expires. Workers share
one `BearerToken`; the lock makes one of them refresh and the rest wait for it,
because Shopware revokes a refresh token once it is used.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

from eval.result_schema import as_list
from functional.oauth import (
    ACCEPT_MCP,
    CLIENT_ENV,
    CONFIG_FILE,
    DEFAULT_CLIENT,
    FIX,
    FIX_URL,
    REDIRECT,
    Reply,
    Send,
    authorization_server_metadata_url,
    pkce,
    query_of,
    resource_metadata_url,
    send_http,
    well_known_metadata_url,
)
from mcp_client import Endpoint

# Refresh this long before the token expires, so a request that starts just
# before expiry cannot arrive with a dead token.
REFRESH_MARGIN_S = 60.0

Clock = Callable[[], float]


class OAuthUnavailable(Exception):
    """The shop cannot do the flow at all: it predates #21862, or the client is
    not registered. A lane fact, reported as a skip naming the cause."""


class OAuthSignInFailed(Exception):
    """The shop advertises OAuth and the sign-in still broke. A finding, never a skip."""


@dataclass(frozen=True)
class Discovery:
    """What the metadata documents said, which is all the grant needs."""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str


def configured_client() -> str:
    return os.environ.get(CLIENT_ENV) or DEFAULT_CLIENT


def _post_form(send: Send, url: str, form: dict[str, str]) -> Reply:
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    return send("POST", url, headers, urlencode(form).encode())


def _post_json(send: Send, url: str, body: dict[str, object], headers: dict[str, str]) -> Reply:
    data = json.dumps(body).encode()
    return send("POST", url, {"Accept": "application/json", "Content-Type": "application/json", **headers}, data)


def discover(mcp_url: str, send: Send = send_http) -> Discovery:
    """Follow the 401 challenge to the two metadata documents."""
    init = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    anonymous = send("POST", mcp_url, {**ACCEPT_MCP, "Content-Type": "application/json"}, init)
    challenge = anonymous.header("WWW-Authenticate")
    named = resource_metadata_url(challenge)
    prm_reply = send("GET", named or well_known_metadata_url(mcp_url), {"Accept": "application/json"}, None)
    prm = prm_reply.json()
    if not named and (prm_reply.status != 200 or not prm):
        raise OAuthUnavailable(
            f"{mcp_url} has no OAuth discovery: the 401 challenge names no resource_metadata and the protected "
            f"resource metadata is not served (HTTP {prm_reply.status}). The shop predates {FIX} ({FIX_URL})"
        )
    if named and prm_reply.status != 200:
        # Not a skip: the shop advertises OAuth and then does not serve it. Seen
        # with mcp/sdk 0.9, which serves the metadata over plain http only on
        # localhost, 127.0.0.1 and ::1, so a `*.localhost` lane answers 404.
        raise OAuthSignInFailed(
            f"the 401 challenge names {named}, which answers HTTP {prm_reply.status}. Over plain http the "
            "metadata may only be served on localhost, 127.0.0.1 or ::1"
        )
    servers = as_list(prm.get("authorization_servers"))
    resource = str(prm.get("resource") or "")
    if prm_reply.status != 200 or not servers or not resource:
        raise OAuthSignInFailed(
            f"the protected resource metadata names no resource or authorization server: HTTP {prm_reply.status} "
            f"{prm_reply.text[:200]}"
        )
    issuer = str(servers[0])
    asm_reply = send("GET", authorization_server_metadata_url(issuer), {"Accept": "application/json"}, None)
    asm = asm_reply.json()
    authorize_url = str(asm.get("authorization_endpoint") or "")
    token_url = str(asm.get("token_endpoint") or "")
    if asm_reply.status != 200 or not authorize_url or not token_url:
        raise OAuthSignInFailed(
            f"the authorization server metadata of {issuer} names no authorization or token endpoint: "
            f"HTTP {asm_reply.status} {asm_reply.text[:200]}"
        )
    methods = [str(m) for m in as_list(asm.get("code_challenge_methods_supported"))]
    if "S256" not in methods:
        raise OAuthSignInFailed(f"the authorization server does not advertise S256 PKCE: {methods}")
    return Discovery(resource, issuer, authorize_url, token_url)


class BearerToken:
    """An access token for the MCP endpoint that refreshes itself. Thread-safe."""

    def __init__(
        self,
        discovery: Discovery,
        client: str,
        reply: Reply,
        send: Send = send_http,
        clock: Clock = time.monotonic,
    ) -> None:
        self.discovery: Discovery = discovery
        self.client: str = client
        self.send: Send = send
        self.clock: Clock = clock
        self.refreshes: int = 0
        self._lock: threading.Lock = threading.Lock()
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.expires_at: float = 0.0
        self._accept(reply, "token request")

    def _accept(self, reply: Reply, what: str) -> None:
        body = reply.json()
        access = str(body.get("access_token") or "")
        if reply.status != 200 or not access:
            raise OAuthSignInFailed(f"the {what} was refused: HTTP {reply.status} {reply.text[:300]}")
        self.access_token = access
        self.refresh_token = str(body.get("refresh_token") or self.refresh_token)
        expires_in = body.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) and expires_in > 0 else 600.0
        self.expires_at = self.clock() + lifetime

    def headers(self) -> dict[str, str]:
        """The Authorization header, refreshed first when the token is about to expire."""
        with self._lock:
            if self.clock() >= self.expires_at - REFRESH_MARGIN_S:
                self._refresh()
            return {"Authorization": f"Bearer {self.access_token}"}

    def _refresh(self) -> None:
        if not self.refresh_token:
            raise OAuthSignInFailed("the access token expired and the token response carried no refresh token")
        reply = _post_form(
            self.send,
            self.discovery.token_endpoint,
            {
                "grant_type": "refresh_token",
                "client_id": self.client,
                "refresh_token": self.refresh_token,
                "resource": self.discovery.resource,
            },
        )
        self._accept(reply, "refresh")
        self.refreshes += 1


def consent_token(discovery: Discovery, access_key: str, secret_key: str, send: Send = send_http) -> str:
    """A token for the principal behind the access key, to post the consent decision with."""
    reply = _post_json(
        send,
        discovery.token_endpoint,
        {"grant_type": "client_credentials", "client_id": access_key, "client_secret": secret_key},
        {},
    )
    token = str(reply.json().get("access_token") or "")
    if not token:
        raise OAuthSignInFailed(
            f"the access key could not sign in to post the consent decision: HTTP {reply.status} {reply.text[:200]}"
        )
    return token


def sign_in(
    endpoint: Endpoint,
    *,
    client: str | None = None,
    send: Send = send_http,
    clock: Clock = time.monotonic,
) -> tuple[Endpoint, BearerToken]:
    """The endpoint, authenticated with an OAuth token instead of its access key.

    The access key is still read, once, to consent. The returned endpoint does
    not carry it, so every MCP request of the run authenticates with the token.
    """
    client = client or configured_client()
    access_key = endpoint.auth_headers.get("sw-access-key", "")
    secret_key = endpoint.auth_headers.get("sw-secret-access-key", "")
    found = discover(f"{endpoint.base_url}{endpoint.path}", send)

    verifier, challenge = pkce()
    state = secrets.token_urlsafe(8)
    request = {
        "response_type": "code",
        "client_id": client,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "write",
        "resource": found.resource,
    }
    asked = send("GET", f"{found.authorization_endpoint}?{urlencode(request)}", {"Accept": "application/json"}, None)
    location = asked.header("Location")
    if asked.status == 403 or "INVALID_ACCESS_KEY" in asked.text or "invalid_client" in location:
        raise OAuthUnavailable(
            f"the public OAuth client {client!r} is not registered on this shop. setup-lane writes it to "
            f"{CONFIG_FILE} under shopware.api.oauth_clients; set {CLIENT_ENV} to use another client"
        )
    if asked.status != 302 or "error=" in location:
        raise OAuthSignInFailed(
            f"the authorization request was refused: HTTP {asked.status} {location or asked.text[:300]}"
        )

    decision = _post_json(
        send,
        found.authorization_endpoint,
        {**request, "approved": True},
        {"Authorization": f"Bearer {consent_token(found, access_key, secret_key, send)}"},
    )
    redirect = str(decision.json().get("redirectUri") or "")
    granted = query_of(redirect)
    if not granted.get("code"):
        raise OAuthSignInFailed(f"the consent decision returned no code: HTTP {decision.status} {decision.text[:300]}")
    if granted.get("state") != state:
        raise OAuthSignInFailed(f"the redirect carried state {granted.get('state')!r}, not the one sent")
    if (iss := granted.get("iss")) and iss != found.issuer:
        raise OAuthSignInFailed(f"the redirect names issuer {iss!r}, the metadata {found.issuer!r} (RFC 9207)")

    token_reply = _post_form(
        send,
        found.token_endpoint,
        {
            "grant_type": "authorization_code",
            "client_id": client,
            "redirect_uri": REDIRECT,
            "code": granted["code"],
            "code_verifier": verifier,
            "resource": found.resource,
        },
    )
    token = BearerToken(found, client, token_reply, send, clock)
    return endpoint.with_bearer(token), token
