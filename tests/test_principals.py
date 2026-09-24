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
        "user-unset": "blocked",
        "user-one-tool": "partial",
    }
    assert keys == ["SWIA", "SWIA", "SWIA", "SWUA", "SWUA"]

    # Unset means NO allowlist write — writing `null` would also be unset, but
    # not writing is what an integration nobody configured actually looks like.
    saves = [p for p in created if p.endswith("/mcp-allowlist")]
    assert len(saves) == 3
    saved = [b["allowlist"] for m, p, b in admin.calls if m == "POST" and p.endswith("/mcp-allowlist")]
    assert saved[0] == P.full_selection(P.AdminApi(BASE, "k", "s"))
    assert saved[1:] == [P.PARTIAL, P.PARTIAL]

    # ACL cannot be what hides a tool from an integration; only the allowlist can.
    integrations = [b for m, p, b in admin.calls if (m, p) == ("POST", "/api/integration")]
    assert all(b["admin"] is True for b in integrations)
    user = admin.body("/api/user")
    assert user["admin"] is False
    assert user["localeId"] == "locale-id"
    assert admin.body("/api/acl-role")["privileges"] == P.PROBE_PRIVILEGES

    deleted = admin.paths("DELETE")
    assert len(deleted) == 6
    assert deleted[-1].startswith("/api/acl-role/"), "the role outlives the users that hold it"


def test_a_failure_half_way_deletes_what_was_already_created(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAdmin(fail="POST /api/user-access-key")
    monkeypatch.setattr(P.requests, "request", fake.request)

    with pytest.raises(P.ProvisioningFailed), P.provisioned(P.AdminApi(BASE, "k", "s")):
        pytest.fail("the body must not run when provisioning failed")

    deleted = fake.paths("DELETE")
    # role, three integrations, the user whose key failed
    assert len(deleted) == 5
    assert deleted[0].startswith("/api/user/")


def test_a_failed_delete_does_not_hide_the_result(admin: FakeAdmin) -> None:
    """Cleanup is best effort. Failing the run over it would bury whatever the
    checks found, and the leftover is recognisable by its label."""
    api = P.AdminApi(BASE, "k", "s")
    admin.fail = "DELETE"

    with P.provisioned(api) as principals:
        assert len(principals) == 5

    assert len(admin.paths("DELETE")) == 6


def test_the_body_raising_still_deletes(admin: FakeAdmin) -> None:
    with pytest.raises(KeyError), P.provisioned(P.AdminApi(BASE, "k", "s")):
        raise KeyError("a check blew up")

    assert len(admin.paths("DELETE")) == 6


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
