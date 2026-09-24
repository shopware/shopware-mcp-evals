#!/usr/bin/env python3
"""Throwaway MCP principals, one per allowlist state, for the allowlist matrix.

shopware/shopware#20600 made the MCP allowlist the gate for every principal
except an administrator user — which is exactly the principal this suite
authenticates as. So the catalogue the suite sees says nothing about what an
integration sees, and "All capabilities is on, `shopware-toolsets-list` returns
`[]`" was reported by a colleague before any check here could have noticed.

This module creates the principals that can notice, through the Admin API, and
deletes them again:

  * an integration and a non-admin user with **no** allowlist — must be blocked
  * an integration with the **full selection** — what the Administration's
    "All capabilities" switch saves for an integration since #20600: not `null`,
    but every name `/api/_action/mcp/capabilities` returned at save time
    (`fullSelection()` in `sw-integration-mcp-allowlist`). Built from the same
    route, so this is the list the switch would have written
  * an integration and a non-admin user with **one** tool — must see that tool
    and nothing from another toolset

The integrations are flagged `admin` so ACL cannot be what hides a tool: only the
allowlist is under test. The users cannot be (an admin user bypasses the
allowlist entirely), so they get a role holding the one privilege the probe call
needs.

Everything is labelled `mcp-evals-…` so a leftover from a crashed run is
recognisable in a dev shop, and `provisioned()` deletes what it created even when
the checks in between raise.
"""

from __future__ import annotations

import secrets
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, cast

import requests

from eval.result_schema import JsonObject, as_list, as_object

TIMEOUT_S = 30

# The probe the partial principals are granted, and the call that exercises it.
# `currency` because a non-admin user needs an entity privilege for the call to
# get past ACL, and currency:read is the smallest one that yields a row on every
# install.
PROBE_TOOL = "shopware-entity-search"
PROBE_ARGS: JsonObject = {"entity": "currency", "limit": 1}
PROBE_PRIVILEGES = ["currency:read"]

Expect = Literal["blocked", "all", "partial"]


class ProvisioningFailed(RuntimeError):
    """A principal could not be created, with the Admin API's answer in the message."""


@dataclass(frozen=True)
class Principal:
    """One MCP credential and what the allowlist should let it reach."""

    label: str
    access_key: str
    secret_key: str
    expect: Expect


class AdminApi:
    """The handful of Admin API calls provisioning needs, authenticated once.

    Uses the suite's own credential via `client_credentials`, which Shopware
    accepts for user access keys as well as integration keys, so no password has
    to be known here.
    """

    def __init__(self, base_url: str, access_key: str, secret_key: str) -> None:
        self.base: str = base_url.rstrip("/")
        status, body = self._send(
            "POST",
            "/api/oauth/token",
            {"grant_type": "client_credentials", "client_id": access_key, "client_secret": secret_key},
            token="",
        )
        token = str(body.get("access_token", ""))
        if status != 200 or not token:
            raise ProvisioningFailed(f"no Admin API token for the suite's credential (HTTP {status})")
        self.token: str = token

    def call(self, method: str, path: str, body: JsonObject | None = None) -> JsonObject:
        status, parsed = self._send(method, path, body, self.token)
        if status >= 300:
            errors = [str(as_object(e).get("detail", "")) for e in as_list(parsed.get("errors"))]
            raise ProvisioningFailed(f"{method} {path} answered HTTP {status}: {'; '.join(errors) or parsed}")
        return parsed

    def _send(self, method: str, path: str, body: JsonObject | None, token: str) -> tuple[int, JsonObject]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = requests.request(
                method,
                f"{self.base}{path}",
                headers=headers,
                json=body,  # pyright: ignore[reportArgumentType]
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise ProvisioningFailed(f"{method} {path} could not be reached: {exc}") from exc
        try:
            return response.status_code, as_object(cast(object, response.json()))
        except ValueError:
            # 204 No Content, which is what every allowlist save answers.
            return response.status_code, {}


def full_selection(api: AdminApi) -> JsonObject:
    """The allowlist the "All capabilities" switch saves for an integration.

    Mirrors `fullSelection()`: tool and prompt names, resource URIs, from the
    route the Administration loads when the dialog opens.
    """
    caps = api.call("GET", "/api/_action/mcp/capabilities")
    return {
        "tools": [str(as_object(t).get("name", "")) for t in as_list(caps.get("tools"))],
        "resources": [str(as_object(r).get("uri", "")) for r in as_list(caps.get("resources"))],
        "prompts": [str(as_object(p).get("name", "")) for p in as_list(caps.get("prompts"))],
    }


PARTIAL: JsonObject = {"tools": [PROBE_TOOL], "resources": [], "prompts": []}


def _keys(prefix: str) -> tuple[str, str]:
    # The shape setup-lane has always minted: prefix + 24 upper-case hex.
    return f"{prefix}{secrets.token_hex(12).upper()}", secrets.token_hex(32)


class _Created:
    """What has been created so far, in the order it has to be deleted."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def add(self, path: str) -> None:
        self.paths.insert(0, path)


def _integration(
    api: AdminApi, created: _Created, label: str, allowlist: JsonObject | None, expect: Expect
) -> Principal:
    integration_id = secrets.token_hex(16)
    access_key, secret_key = _keys("SWIA")
    api.call(
        "POST",
        "/api/integration",
        {
            "id": integration_id,
            "label": label,
            "accessKey": access_key,
            "secretAccessKey": secret_key,
            "admin": True,
            "writeAccess": True,
        },
    )
    created.add(f"/api/integration/{integration_id}")
    if allowlist is not None:
        api.call("POST", f"/api/_action/integration/{integration_id}/mcp-allowlist", {"allowlist": allowlist})
    return Principal(label, access_key, secret_key, expect)


def _locale_id(api: AdminApi) -> str:
    body = api.call(
        "POST", "/api/search/locale", {"limit": 1, "filter": [{"type": "equals", "field": "code", "value": "en-GB"}]}
    )
    rows = as_list(body.get("data")) or as_list(api.call("POST", "/api/search/locale", {"limit": 1}).get("data"))
    if not rows:
        raise ProvisioningFailed("no locale on this instance to create a user with")
    return str(as_object(rows[0]).get("id", ""))


def _user(
    api: AdminApi,
    created: _Created,
    label: str,
    role_id: str,
    locale_id: str,
    allowlist: JsonObject | None,
    expect: Expect,
) -> Principal:
    user_id = secrets.token_hex(16)
    access_key, secret_key = _keys("SWUA")
    api.call(
        "POST",
        "/api/user",
        {
            "id": user_id,
            "username": label,
            "email": f"{label}@example.invalid",
            "firstName": "mcp-evals",
            "lastName": label,
            "password": secrets.token_hex(16),
            "localeId": locale_id,
            "admin": False,
            "aclRoles": [{"id": role_id}],
        },
    )
    created.add(f"/api/user/{user_id}")
    api.call(
        "POST", "/api/user-access-key", {"userId": user_id, "accessKey": access_key, "secretAccessKey": secret_key}
    )
    if allowlist is not None:
        api.call("POST", f"/api/_action/user/{user_id}/mcp-allowlist", {"allowlist": allowlist})
    return Principal(label, access_key, secret_key, expect)


@contextmanager
def provisioned(api: AdminApi) -> Generator[list[Principal]]:
    """Create the matrix, yield it, and delete it again whatever happens."""
    created = _Created()
    run = secrets.token_hex(3)
    try:
        role_id = secrets.token_hex(16)
        api.call("POST", "/api/acl-role", {"id": role_id, "name": f"mcp-evals-{run}", "privileges": PROBE_PRIVILEGES})
        created.add(f"/api/acl-role/{role_id}")
        locale_id = _locale_id(api)

        yield [
            _integration(api, created, f"mcp-evals-{run}-integration-unset", None, "blocked"),
            _integration(api, created, f"mcp-evals-{run}-integration-all", full_selection(api), "all"),
            _integration(api, created, f"mcp-evals-{run}-integration-one-tool", PARTIAL, "partial"),
            _user(api, created, f"mcp-evals-{run}-user-unset", role_id, locale_id, None, "blocked"),
            _user(api, created, f"mcp-evals-{run}-user-one-tool", role_id, locale_id, PARTIAL, "partial"),
        ]
    finally:
        # Users before the role they hold; the list is already newest-first.
        for path in created.paths:
            try:
                api.call("DELETE", path)
            except ProvisioningFailed:
                # A leftover is recognisable by its label; failing the run over
                # cleanup would hide whatever the checks found.
                pass
