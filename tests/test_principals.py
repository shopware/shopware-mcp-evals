"""Provisioning the allowlist matrix's principals through the Admin API.

What matters: the "All" principal gets exactly what the Administration's switch
would save, an unset principal gets no allowlist written at all, and whatever
was created is deleted again — including when creation fails half-way.
"""

from typing import cast

import pytest
import requests

from eval.result_schema import JsonObject
from functional import principals as P

BASE = "http://shop.example"

CAPABILITIES: JsonObject = {
    "tools": [{"name": "shopware-entity-search", "title": "Search"}, {"name": "shopware-order-state"}],
    "resources": [{"uri": "shopware://entities", "name": "entities"}],
    "prompts": [{"name": "shopware-context"}],
}


class FakeResponse:
    def __init__(self, status: int, body: JsonObject | None) -> None:
        self.status_code: int = status
        self._body: JsonObject | None = body

    def json(self) -> JsonObject:
        if self._body is None:
            raise ValueError("no body")  # what requests raises for a 204
        return self._body


class FakeAdmin:
    """The Admin API paths provisioning touches. Everything else answers 204."""

    def __init__(self, *, fail: str = "", locales: list[JsonObject] | None = None) -> None:
        self.fail: str = fail
        self.locales: list[JsonObject] = [{"id": "locale-id"}] if locales is None else locales
        self.calls: list[tuple[str, str, JsonObject]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        path = url.removeprefix(BASE)
        body = cast(JsonObject, kwargs.get("json")) or {}
        self.calls.append((method, path, body))
        if self.fail and f"{method} {path}".startswith(self.fail):
            return FakeResponse(400, {"errors": [{"detail": "This value should not be blank."}]})
        if path == "/api/oauth/token":
            return FakeResponse(200, {"access_token": "token"})
        if path == "/api/_action/mcp/capabilities":
            return FakeResponse(200, CAPABILITIES)
        if path == "/api/search/locale":
            wants_en = bool(body.get("filter"))
            return FakeResponse(200, {"data": [] if wants_en and len(self.locales) > 1 else self.locales[:1]})
        return FakeResponse(204, None)

    def paths(self, method: str) -> list[str]:
        return [p for m, p, _ in self.calls if m == method]

    def body(self, path_prefix: str) -> JsonObject:
        return next(b for _, p, b in self.calls if p.startswith(path_prefix))


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> FakeAdmin:
    fake = FakeAdmin()
    monkeypatch.setattr(P.requests, "request", fake.request)
    return fake


def test_the_token_comes_from_client_credentials(admin: FakeAdmin) -> None:
    api = P.AdminApi(BASE + "/", "SWUAKEY", "secret")

    assert api.token == "token"
    assert admin.calls[0] == (
        "POST",
        "/api/oauth/token",
        {"grant_type": "client_credentials", "client_id": "SWUAKEY", "client_secret": "secret"},
    )


def test_a_refused_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAdmin(fail="POST /api/oauth/token")
    monkeypatch.setattr(P.requests, "request", fake.request)

    with pytest.raises(P.ProvisioningFailed, match="HTTP 400"):
        _ = P.AdminApi(BASE, "SWUAKEY", "secret")


def test_an_unreachable_server_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(*_args: object, **_kwargs: object) -> FakeResponse:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(P.requests, "request", request)

    with pytest.raises(P.ProvisioningFailed, match="could not be reached"):
        _ = P.AdminApi(BASE, "SWUAKEY", "secret")


def test_an_error_names_the_admin_apis_detail(admin: FakeAdmin) -> None:
    api = P.AdminApi(BASE, "k", "s")
    admin.fail = "POST /api/user"

    with pytest.raises(P.ProvisioningFailed, match="POST /api/user answered HTTP 400: This value should not be blank."):
        _ = api.call("POST", "/api/user", {})


def test_full_selection_is_what_the_all_switch_saves(admin: FakeAdmin) -> None:
    """`fullSelection()`: tool and prompt names, resource URIs."""
    selection = P.full_selection(P.AdminApi(BASE, "k", "s"))

    assert selection == {
        "tools": ["shopware-entity-search", "shopware-order-state"],
        "resources": ["shopware://entities"],
        "prompts": ["shopware-context"],
    }
    assert ("GET", "/api/_action/mcp/capabilities", {}) in admin.calls


def test_the_matrix_is_created_as_described_and_deleted_afterwards(admin: FakeAdmin) -> None:
    with P.provisioned(P.AdminApi(BASE, "k", "s")) as principals:
        expects = {p.label.split("-", 3)[-1]: p.expect for p in principals}
        keys = [p.access_key[:4] for p in principals]
        created = admin.paths("POST")

    assert expects == {
        "integration-unset": "blocked",
        "integration-all": "all",
        "integration-one-tool": "partial",
        "integration-acl-one-tool": "partial",
        "user-unset": "blocked",
        "user-one-tool": "partial",
        "user-all": "all",
        "admin-one-tool": "all",
        "admin-empty": "all",
    }
    assert keys == ["SWIA"] * 4 + ["SWUA"] * 5
    assert [p.acl for p in principals] == [False, False, False, True, True, True, True, False, False]
    assert [p.granted for p in principals if p.expect == "partial"] == [P.PARTIAL_TOOLS] * 3

    # Unset means NO allowlist write — writing `null` would also be unset, but
    # not writing is what an integration nobody configured actually looks like.
    saves = [p for p in created if p.endswith("/mcp-allowlist")]
    assert len(saves) == 7
    everything = P.full_selection(P.AdminApi(BASE, "k", "s"))
    saved = [b["allowlist"] for m, p, b in admin.calls if m == "POST" and p.endswith("/mcp-allowlist")]
    assert saved == [everything, P.PARTIAL, P.PARTIAL, P.PARTIAL, everything, P.PARTIAL, P.EMPTY]

    # Only the integration given a role is held to it; the rest are flagged
    # admin, so only the allowlist can hide a tool from them.
    integrations = [b for m, p, b in admin.calls if (m, p) == ("POST", "/api/integration")]
    assert [b["admin"] for b in integrations] == [True, True, True, False]
    assert "aclRoles" in integrations[3] and "aclRoles" not in integrations[0]
    users = [b for m, p, b in admin.calls if (m, p) == ("POST", "/api/user")]
    assert [u["admin"] for u in users] == [False, False, False, True, True]
    assert ["aclRoles" in u for u in users] == [True, True, True, False, False]
    assert users[0]["localeId"] == "locale-id"
    assert admin.body("/api/acl-role")["privileges"] == P.PROBE_PRIVILEGES

    deleted = admin.paths("DELETE")
    assert len(deleted) == 10
    assert deleted[-1].startswith("/api/acl-role/"), "the role outlives the users that hold it"


def test_a_failure_half_way_deletes_what_was_already_created(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAdmin(fail="POST /api/user-access-key")
    monkeypatch.setattr(P.requests, "request", fake.request)

    with pytest.raises(P.ProvisioningFailed), P.provisioned(P.AdminApi(BASE, "k", "s")):
        pytest.fail("the body must not run when provisioning failed")

    deleted = fake.paths("DELETE")
    # role, four integrations, the user whose key failed
    assert len(deleted) == 6
    assert deleted[0].startswith("/api/user/")


def test_a_failed_delete_does_not_hide_the_result(admin: FakeAdmin) -> None:
    """Cleanup is best effort. Failing the run over it would bury whatever the
    checks found, and the leftover is recognisable by its label."""
    api = P.AdminApi(BASE, "k", "s")
    admin.fail = "DELETE"

    with P.provisioned(api) as principals:
        assert len(principals) == 9

    assert len(admin.paths("DELETE")) == 10


def test_the_body_raising_still_deletes(admin: FakeAdmin) -> None:
    with pytest.raises(KeyError), P.provisioned(P.AdminApi(BASE, "k", "s")):
        raise KeyError("a check blew up")

    assert len(admin.paths("DELETE")) == 10


def test_a_missing_en_gb_locale_falls_back_to_any(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAdmin(locales=[{"id": "de-id"}, {"id": "other"}])
    monkeypatch.setattr(P.requests, "request", fake.request)

    with P.provisioned(P.AdminApi(BASE, "k", "s")):
        pass

    assert fake.body("/api/user")["localeId"] == "de-id"


def test_no_locale_at_all_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAdmin(locales=[])
    monkeypatch.setattr(P.requests, "request", fake.request)

    with pytest.raises(P.ProvisioningFailed, match="no locale"), P.provisioned(P.AdminApi(BASE, "k", "s")):
        pass


def test_another_principal_is_one_integration_granted_everything_and_deleted(admin: FakeAdmin) -> None:
    api = P.AdminApi(BASE, "SWUAKEY", "secret")

    with P.another_principal(api) as other:
        assert other.access_key.startswith("SWIA")
        assert admin.paths("DELETE") == []

    created = admin.body("/api/integration")
    assert admin.paths("DELETE") == [f"/api/integration/{created['id']}"]
    assert admin.body("/api/_action/integration/") == {"allowlist": P.full_selection(api)}


def test_another_principal_is_deleted_when_the_check_raises(admin: FakeAdmin) -> None:
    api = P.AdminApi(BASE, "SWUAKEY", "secret")

    with pytest.raises(RuntimeError), P.another_principal(api):
        raise RuntimeError("check blew up")

    assert len(admin.paths("DELETE")) == 1


# --- the OAuth sign-in users --------------------------------------------------------


def test_the_sign_in_users_get_a_password_a_role_and_an_allowlist_each(admin: FakeAdmin) -> None:
    api = P.AdminApi(BASE, "SWUAKEY", "secret")
    everything = P.full_selection(api)

    with P.sign_in_users(api) as signed:
        assert [u.label.split("-", 3)[-1] for u in signed] == [
            "oauth-user-unset",
            "oauth-user-one-tool",
            "oauth-user-all",
            "oauth-admin-one-tool",
        ]
        assert [u.expect for u in signed] == ["blocked", "partial", "all", "all"]
        users = [b for m, p, b in admin.calls if (m, p) == ("POST", "/api/user")]
        assert [u["username"] for u in users] == [u.username for u in signed]
        assert [u["password"] for u in users] == [u.password for u in signed]
        assert [u["admin"] for u in users] == [False, False, False, True]
        assert admin.body("/api/acl-role")["privileges"] == P.PROBE_PRIVILEGES
        allowlists = [b["allowlist"] for m, p, b in admin.calls if m == "POST" and p.endswith("/mcp-allowlist")]
        assert allowlists == [P.PARTIAL, everything, P.PARTIAL]
        assert "/api/user-access-key" not in admin.paths("POST")

    deleted = admin.paths("DELETE")
    assert len(deleted) == 5 and deleted[-1].startswith("/api/acl-role/")


def test_a_sign_in_user_becomes_a_bearer_principal() -> None:
    one = P.SignInUser("mcp-evals-x-oauth-user-one-tool", "u", "pw", P.PARTIAL, "partial")
    admin = P.SignInUser("mcp-evals-x-oauth-admin-one-tool", "a", "pw", P.PARTIAL, "all", admin=True)

    assert one.principal("t") == P.Principal(
        one.label, "", "", "partial", acl=True, granted=P.PARTIAL_TOOLS, bearer="t"
    )
    assert admin.principal("t") == P.Principal(admin.label, "", "", "all", acl=False, bearer="t")


def test_the_sign_in_users_are_deleted_when_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAdmin(fail="POST /api/_action/user/")
    monkeypatch.setattr(P.requests, "request", fake.request)
    api = P.AdminApi(BASE, "SWUAKEY", "secret")

    with pytest.raises(P.ProvisioningFailed), P.sign_in_users(api):
        pass

    assert [p.split("/")[2] for p in fake.paths("DELETE")] == ["user", "user", "acl-role"]
