"""Signing in over OAuth for a whole eval run, against a fake shop.

The fake serves its authorization and token endpoints at paths no Shopware
uses, so a client that hard-codes `/api/oauth/authorize` or `/api/oauth/token`
fails here instead of passing by coincidence.
"""

import argparse
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from eval import oauth_login as L
from eval import runner as E
from eval import summary as S
from eval.result_schema import JsonObject, SummaryRow, as_object
from functional.oauth import Reply
from mcp_client import Endpoint, admin_endpoint
from tests.stubs import const, never, raiser

BASE = "http://shop.example"
MCP = f"{BASE}/api/_mcp"
ISSUER = f"{BASE}/api"
AUTHORIZE = f"{BASE}/oauth2/auth"
TOKEN = f"{BASE}/oauth2/token"
ENDPOINT: Endpoint = admin_endpoint("SWUAkey", "secret", base_url=BASE)


def reply(status: int, body: object = None, **headers: str) -> Reply:
    text = body if isinstance(body, str) else ("" if body is None else json.dumps(body))
    return Reply(status, {k.lower().replace("_", "-"): v for k, v in headers.items()}, text)


class Clock:
    def __init__(self) -> None:
        self.now: float = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeShop:
    """The discovery documents, the grant and the refresh, with a switch per way to break."""

    def __init__(self, **broken: bool) -> None:
        self.broken: dict[str, bool] = broken
        self.requests: list[tuple[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.issued: int = 0
        self.lock: threading.Lock = threading.Lock()

    def has(self, flag: str) -> bool:
        return self.broken.get(flag, False)

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Reply:
        parts = urlsplit(url)
        path, query = parts.path, dict(parse_qsl(parts.query))
        with self.lock:
            self.requests.append((method, path))
        text = (body or b"").decode()
        form: dict[str, str] = (
            dict(parse_qsl(text))
            if headers.get("Content-Type") == "application/x-www-form-urlencoded"
            else {k: str(v) for k, v in as_object(cast(object, json.loads(text) if text else {})).items()}
        )
        if path == "/api/_mcp":
            if self.has("no_challenge") or self.has("no_discovery"):
                return reply(401, "Unauthorized")
            prm = f"{BASE}/.well-known/oauth-protected-resource/api/_mcp"
            return reply(401, "Unauthorized", WWW_Authenticate=f'Bearer resource_metadata="{prm}"')
        if path == "/.well-known/oauth-protected-resource/api/_mcp":
            if self.has("no_discovery") or self.has("metadata_gone"):
                return reply(404, "Not Found")
            if self.has("no_servers"):
                return reply(200, {"resource": MCP})
            return reply(200, {"resource": MCP, "authorization_servers": [ISSUER]})
        if path == "/.well-known/oauth-authorization-server/api":
            asm: JsonObject = {"issuer": ISSUER, "code_challenge_methods_supported": ["S256"]}
            if not self.has("no_endpoints"):
                asm |= {"authorization_endpoint": AUTHORIZE, "token_endpoint": TOKEN}
            if self.has("no_s256"):
                asm["code_challenge_methods_supported"] = ["plain"]
            return reply(200, asm)
        if path == "/oauth2/auth":
            return self.authorize(method, query, form, headers)
        if path == "/oauth2/token":
            with self.lock:
                self.forms.append(form)
                return self.token(form)
        return reply(404, "Not Found")

    def authorize(self, method: str, query: dict[str, str], form: dict[str, str], headers: Mapping[str, str]) -> Reply:
        if method == "GET":
            if self.has("unregistered"):
                return reply(403, '{"errors":[{"code":"FRAMEWORK__INVALID_ACCESS_KEY"}]}')
            if self.has("refused"):
                return reply(302, Location="http://127.0.0.1/callback?error=invalid_target")
            assert query["resource"] == MCP
            assert query["code_challenge_method"] == "S256"
            return reply(302, Location=f"{BASE}/admin#/oauth/authorize?{urlencode(query)}")
        assert headers.get("Authorization") == "Bearer consent-token"
        assert form["approved"] == "True"
        if self.has("no_code"):
            return reply(400, {"errors": []})
        state = "other" if self.has("wrong_state") else form["state"]
        iss = "http://evil.example/api" if self.has("wrong_iss") else ISSUER
        redirect = f"{form['redirect_uri']}?{urlencode({'code': 'the-code', 'state': state, 'iss': iss})}"
        return reply(200, {"redirectUri": redirect})

    def token(self, form: dict[str, str]) -> Reply:
        grant = form.get("grant_type")
        if grant == "client_credentials":
            if form.get("client_id") != "SWUAkey" or self.has("bad_key"):
                return reply(401, {"errors": []})
            return reply(200, {"access_token": "consent-token"})
        if grant == "authorization_code":
            assert form["resource"] == MCP
            assert form["code"] == "the-code"
            if self.has("token_refused"):
                return reply(400, {"error": "invalid_grant"})
            self.issued += 1
            body: JsonObject = {"access_token": f"access-{self.issued}", "expires_in": 600}
            if not self.has("no_refresh_token"):
                body["refresh_token"] = f"refresh-{self.issued}"
            return reply(200, body)
        if grant == "refresh_token":
            assert form["resource"] == MCP
            if self.has("refresh_refused") or form["refresh_token"] != f"refresh-{self.issued}":
                return reply(401, {"error": "invalid_grant"})
            self.issued += 1
            return reply(200, {"access_token": f"access-{self.issued}", "refresh_token": f"refresh-{self.issued}"})
        return reply(400, {"error": "unsupported_grant_type"})


def sign_in(shop: FakeShop, clock: Clock | None = None) -> tuple[Endpoint, L.BearerToken]:
    return L.sign_in(ENDPOINT, client="mcp-evals-probe", send=shop, clock=clock or Clock())


# -- discovery ---------------------------------------------------------------
def test_the_grant_uses_the_endpoints_the_metadata_names() -> None:
    shop = FakeShop()
    endpoint, token = sign_in(shop)

    assert token.discovery == L.Discovery(MCP, ISSUER, AUTHORIZE, TOKEN)
    assert ("GET", "/oauth2/auth") in shop.requests
    assert not any(path.startswith("/api/oauth") for _, path in shop.requests)
    assert endpoint.request_headers()["Authorization"] == "Bearer access-1"


def test_without_a_challenge_the_well_known_url_is_tried() -> None:
    assert L.discover(MCP, FakeShop(no_challenge=True)).token_endpoint == TOKEN


def test_a_shop_without_discovery_is_unavailable_naming_the_pr() -> None:
    with pytest.raises(L.OAuthUnavailable, match="21862"):
        _ = L.discover(MCP, FakeShop(no_discovery=True))


@pytest.mark.parametrize(
    ("flag", "says"),
    [
        (
            "metadata_gone",
            "names http://shop.example/.well-known/oauth-protected-resource/api/_mcp, which answers HTTP 404",
        ),
        ("no_servers", "no resource or authorization server"),
        ("no_endpoints", "no authorization or token endpoint"),
        ("no_s256", "S256"),
    ],
)
def test_broken_metadata_is_a_failure_not_a_skip(flag: str, says: str) -> None:
    with pytest.raises(L.OAuthSignInFailed, match=says):
        _ = L.discover(MCP, FakeShop(**{flag: True}))


# -- the grant ---------------------------------------------------------------
def test_the_code_is_exchanged_with_the_verifier_and_the_resource() -> None:
    shop = FakeShop()
    _ = sign_in(shop)

    exchange = next(f for f in shop.forms if f["grant_type"] == "authorization_code")
    assert exchange["client_id"] == "mcp-evals-probe"
    assert len(exchange["code_verifier"]) >= 43


def test_an_unregistered_client_is_unavailable() -> None:
    with pytest.raises(L.OAuthUnavailable, match="not registered"):
        _ = sign_in(FakeShop(unregistered=True))


@pytest.mark.parametrize(
    ("flag", "says"),
    [
        ("refused", "authorization request was refused"),
        ("bad_key", "could not sign in to post the consent"),
        ("no_code", "returned no code"),
        ("wrong_state", "state"),
        ("wrong_iss", "RFC 9207"),
        ("token_refused", "token request was refused"),
    ],
)
def test_a_broken_grant_is_a_failure(flag: str, says: str) -> None:
    with pytest.raises(L.OAuthSignInFailed, match=says):
        _ = sign_in(FakeShop(**{flag: True}))


def test_the_client_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_EVALS_OAUTH_CLIENT", "another-client")
    assert L.configured_client() == "another-client"
    monkeypatch.delenv("MCP_EVALS_OAUTH_CLIENT")
    assert L.configured_client() == "mcp-evals-probe"


# -- the token over a run ----------------------------------------------------
def test_the_token_is_refreshed_shortly_before_it_expires() -> None:
    shop, clock = FakeShop(), Clock()
    _, token = sign_in(shop, clock)

    clock.now += 600 - L.REFRESH_MARGIN_S - 1
    assert token.headers() == {"Authorization": "Bearer access-1"}
    clock.now += 2
    assert token.headers() == {"Authorization": "Bearer access-2"}
    assert token.refreshes == 1
    # No expires_in on the refresh: ten minutes, as Shopware's default.
    assert token.expires_at == pytest.approx(clock.now + 600)


def test_workers_share_one_refresh() -> None:
    """Shopware revokes a used refresh token, so a second refresh would fail the run."""
    shop, clock = FakeShop(), Clock()
    _, token = sign_in(shop, clock)
    clock.now += 600

    seen: list[str] = []
    workers = [threading.Thread(target=lambda: seen.append(token.headers()["Authorization"])) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert set(seen) == {"Bearer access-2"}
    assert token.refreshes == 1


def test_a_refused_refresh_fails_the_run() -> None:
    shop, clock = FakeShop(), Clock()
    _, token = sign_in(shop, clock)
    shop.broken["refresh_refused"] = True
    clock.now += 600

    with pytest.raises(L.OAuthSignInFailed, match="refresh was refused"):
        _ = token.headers()


def test_an_expired_token_without_a_refresh_token_fails_the_run() -> None:
    clock = Clock()
    _, token = sign_in(FakeShop(no_refresh_token=True), clock)
    clock.now += 600

    with pytest.raises(L.OAuthSignInFailed, match="no refresh token"):
        _ = token.headers()


# -- the endpoint ------------------------------------------------------------
def test_the_endpoint_drops_the_access_key_and_its_clones_keep_the_token() -> None:
    endpoint, _ = sign_in(FakeShop())
    pinned = endpoint.with_toolsets("all").with_protocol("2024-11-05")

    for each in (endpoint, pinned):
        headers = each.request_headers()
        assert "sw-access-key" not in headers
        assert "sw-secret-access-key" not in headers
        assert headers["Authorization"] == "Bearer access-1"
    assert pinned.url == f"{MCP}?toolsets=all"
    assert ENDPOINT.request_headers()["sw-access-key"] == "SWUAkey"


# -- the runner --------------------------------------------------------------
def args(**over: object) -> argparse.Namespace:
    return E.build_parser().parse_args([f"--{k.replace('_', '-')}={v}" for k, v in over.items()])


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "SW_SC_ACCESS_KEY"):
        monkeypatch.setattr(E, name, "test-value")
    monkeypatch.setattr(E, "build_client", const(object()))


@pytest.mark.usefixtures("credentials")
def test_oauth_on_the_store_endpoint_is_a_config_error() -> None:
    with pytest.raises(E.ConfigError, match="admin endpoint only"):
        _ = E.run_suite(args(provider="openai", endpoint="store", auth="oauth"))


@pytest.mark.usefixtures("credentials")
def test_a_shop_without_oauth_records_a_skip_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(E.oauth_login, "sign_in", raiser(L.OAuthUnavailable("predates shopware/shopware#21862")))
    monkeypatch.setattr(E, "run_discovery_pass", never("no fixture may run without a token"))
    row_path = tmp_path / "rows" / "7-admin-oauth.json"

    parsed = args(provider="openai", auth="oauth", summary_row=str(row_path), suite_label="admin · OAuth")
    parsed.advisory = True
    code = E.run_suite(parsed)

    assert code == E.OAUTH_SKIP_EXIT
    assert code not in (0, 1, E.CRASH_EXIT)
    assert "::warning::admin · OAuth skipped, OAuth is unavailable: predates" in capsys.readouterr().out
    row = cast(SummaryRow, json.loads(row_path.read_text()))
    assert (row["gate"], row["graded"], row.get("skip_reason")) == ("SKIP", 0, "predates shopware/shopware#21862")
    assert "SKIP (advisory): predates shopware/shopware#21862" in S.render_runs([row])


@pytest.mark.usefixtures("credentials")
def test_the_whole_run_uses_the_token_and_the_report_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shop = FakeShop()
    real_sign_in = L.sign_in

    def fake_sign_in(endpoint: Endpoint) -> tuple[Endpoint, L.BearerToken]:
        assert endpoint.name == "admin"
        return real_sign_in(admin_endpoint("SWUAkey", "secret", base_url=BASE), send=shop, clock=Clock())

    used: list[Endpoint] = []

    def capture(endpoint: Endpoint, **_kwargs: object) -> tuple[str, JsonObject]:
        used.append(endpoint)
        return "", {}

    def catalogue(endpoint: Endpoint) -> set[str]:
        used.append(endpoint)
        return set()

    monkeypatch.setattr(E.oauth_login, "sign_in", fake_sign_in)
    monkeypatch.setattr(E, "fetch_system_prompt", capture)
    monkeypatch.setattr(E, "probe_catalogue", catalogue)
    monkeypatch.setattr(E, "load_fixtures", const([{"id": "x", "prompt": "p", "expected_tool": "shopware-x"}]))
    monkeypatch.setattr(E, "resolve_lane_substitutions", const(dict[str, str]()))
    monkeypatch.setattr(E, "run_discovery_pass", const([]))
    out = tmp_path / "r.json"

    code = E.run_suite(args(provider="openai", auth="oauth", output=str(out)))

    assert code in (0, 1)
    printed = capsys.readouterr().out
    assert "Auth:     OAuth, client 'mcp-evals-probe'" in printed
    assert "OAuth token refreshed 0 time(s)" in printed
    assert used
    assert all(e.request_headers().get("Authorization") == "Bearer access-1" for e in used)
    assert all("sw-access-key" not in e.request_headers() for e in used)
    assert json.loads(out.read_text())["auth"] == "oauth"


def test_a_failed_sign_in_is_a_crash_not_a_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(E, "run_suite", raiser(L.OAuthSignInFailed("the refresh was refused")))
    monkeypatch.setattr("sys.argv", ["runner", "--provider", "openai"])

    assert E.main() == E.CRASH_EXIT
    assert "the OAuth sign-in failed: the refresh was refused" in capsys.readouterr().err


def test_keys_stay_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_AUTH", raising=False)
    assert cast(str, args().auth) == "keys"
    report = E.build_report("openai", "m", [], None, True, 6)
    assert report.get("auth") == "keys"
