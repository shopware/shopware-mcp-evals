"""An app's MCP tool, prompt and resource, end to end.

Shopware lets an app add MCP capabilities in `Resources/mcp.xml`. The shop
proxies each one to the app's URL as a signed POST and hands the app's answer
to the client. This module installs `functional/app_fixture.py`'s app, which
declares one capability of each kind, and checks that a client gets the app's
own content back:

  tools/call      the tool's result carries the text the call sent
  prompts/get     the prompt is the app's one user message
  resources/read  the resource's text is the app's `text`, not its whole answer

The fixture is uninstalled and removed again in a `finally`. It must not stay:
the LLM eval fetches every prompt the server lists.

It changes the shop, so it only runs with `--app-fixture`. The shop must be
allowed to call the app server, which listens on plain HTTP on a private
address. setup-lane writes the narrow config for that
(`shopware.app_system.allow_unencrypted_traffic` and
`allowed_private_ip_addresses`), and the README says how to do the same on a
local lane.

The prompt and resource checks fail on a shop without shopware/shopware#22623.
They are registered in `functional/known_failures.py`, so they run and are
reported as known until the pinned Shopware carries the fix.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from typing import cast

from eval.result_schema import McpResponse, as_list, as_object
from functional.app_fixture import (
    APP_NAME,
    PROMPT_NAME,
    PROMPT_TEXT,
    RESOURCE_MIME_TYPE,
    RESOURCE_TEXT,
    RESOURCE_URI,
    TOOL_NAME,
    AppServer,
    render_app,
)
from functional.principals import AdminApi, ProvisioningFailed
from functional.reporting import Reporter
from mcp_client import (
    SW_ACCESS_KEY,
    SW_SECRET_ACCESS_KEY,
    Endpoint,
    enable_toolset,
    mcp_call,
    mcp_get_prompt,
    mcp_init,
    mcp_list_names,
    mcp_read_resource,
)
from mcp_result import read_result

SECTION = "App MCP capabilities"
INSTALL_CHECK = "the fixture app installs and registers"
TOOL_CHECK = "an app MCP tool answers with the app's result"
LISTED_CHECK = "an app MCP prompt and resource are listed"
PROMPT_CHECK = "an app MCP prompt answers with the app's messages"
RESOURCE_CHECK = "an app MCP resource answers with the app's text"
CLEANUP_CHECK = "the fixture app is removed again"
ALL_CHECKS = "app MCP tool, prompt and resource"

NEEDS_FLAG = (
    "needs --app-fixture: it installs a throwaway app and serves it from this process, "
    "and the shop must be allowed to call that server over plain HTTP"
)
# The fragment known_failures.py matches. A server without the fix forwards the
# app's whole JSON answer as the resource text.
WHOLE_ENVELOPE = "the resource text is the app's whole response envelope"

EXTENSION = "/api/_action/extension"


def _error(resp: McpResponse) -> str | None:
    """`JSON-RPC <code>: <message>` for an error reply, None for a result."""
    if "error" not in resp:
        return None
    error = as_object(resp.get("error"))
    return f"JSON-RPC {error.get('code')}: {error.get('message', 'no message')}"


def install(api: AdminApi, archive: bytes) -> None:
    """Upload, install and activate the fixture, after clearing out a leftover."""
    remove(api, quiet=True)
    api.upload(f"{EXTENSION}/upload", f"{APP_NAME}.zip", archive)
    api.call("POST", f"{EXTENSION}/install/app/{APP_NAME}")
    api.call("PUT", f"{EXTENSION}/activate/app/{APP_NAME}")


def remove(api: AdminApi, *, quiet: bool = False) -> None:
    """Uninstall and delete the fixture. `quiet` for the pre-install sweep, where
    there is usually nothing to remove and the refusal says so."""
    for action in ("uninstall", "remove"):
        try:
            api.call("POST", f"{EXTENSION}/{action}/app/{APP_NAME}", {"keepUserData": False})
        except ProvisioningFailed:
            if not quiet:
                raise


def check_tool(rep: Reporter, session: str, endpoint: Endpoint, server: AppServer) -> None:
    nonce = secrets.token_hex(8)
    enabled = enable_toolset(session, APP_NAME, endpoint)
    if (error := _error(enabled)) is not None:
        rep.check_fail(TOOL_CHECK, f"shopware-toolset-enable {APP_NAME} refused: {error}")
        return
    resp = mcp_call(session, TOOL_NAME, {"text": nonce}, endpoint)
    if (error := _error(resp)) is not None:
        rep.check_fail(TOOL_CHECK, f"tools/call {TOOL_NAME} answered {error}")
        return
    outcome = read_result(resp)
    if not outcome.ok:
        rep.check_fail(TOOL_CHECK, f"{TOOL_NAME} failed: {outcome.failure}{_reached(server, '/tool')}")
        return
    if as_object(outcome.data).get("echo") != nonce:
        rep.check_fail(TOOL_CHECK, f"{TOOL_NAME} did not echo {nonce}: {outcome.text[:300]}")
        return
    rep.check_pass(TOOL_CHECK)


def check_listed(rep: Reporter, session: str, endpoint: Endpoint) -> None:
    missing: list[str] = []
    if PROMPT_NAME not in mcp_list_names(session, "prompts/list", endpoint):
        missing.append(f"prompts/list has no {PROMPT_NAME}")
    if RESOURCE_URI not in mcp_list_names(session, "resources/list", endpoint):
        missing.append(f"resources/list has no {RESOURCE_URI}")
    if missing:
        rep.check_fail(LISTED_CHECK, "; ".join(missing))
    else:
        rep.check_pass(LISTED_CHECK)


def check_prompt(rep: Reporter, session: str, endpoint: Endpoint, server: AppServer) -> None:
    resp = mcp_get_prompt(session, PROMPT_NAME, endpoint)
    if (error := _error(resp)) is not None:
        rep.check_fail(PROMPT_CHECK, f"prompts/get {PROMPT_NAME} answered {error}{_reached(server, '/prompt')}")
        return
    messages = as_list(as_object(resp.get("result")).get("messages"))
    first = as_object(messages[0]) if messages else {}
    content = as_object(first.get("content"))
    if len(messages) != 1 or first.get("role") != "user" or content.get("text") != PROMPT_TEXT:
        rep.check_fail(PROMPT_CHECK, f"expected one user message with the app's text, got {json.dumps(messages)[:300]}")
        return
    rep.check_pass(PROMPT_CHECK)


def check_resource(rep: Reporter, session: str, endpoint: Endpoint, server: AppServer) -> None:
    resp = mcp_read_resource(session, RESOURCE_URI, endpoint)
    if (error := _error(resp)) is not None:
        rep.check_fail(RESOURCE_CHECK, f"resources/read {RESOURCE_URI} answered {error}{_reached(server, '/resource')}")
        return
    contents = as_list(as_object(resp.get("result")).get("contents"))
    first = as_object(contents[0]) if contents else {}
    text = first.get("text")
    if text == RESOURCE_TEXT and first.get("mimeType") == RESOURCE_MIME_TYPE:
        rep.check_pass(RESOURCE_CHECK)
        return
    if isinstance(text, str) and is_envelope(text):
        rep.check_fail(RESOURCE_CHECK, f"{WHOLE_ENVELOPE}, not its text: {text[:300]}")
        return
    rep.check_fail(
        RESOURCE_CHECK, f"expected {RESOURCE_MIME_TYPE} text {RESOURCE_TEXT}, got {json.dumps(contents)[:300]}"
    )


def is_envelope(text: str) -> bool:
    """Whether a resource text is the app's `{"uri", "text", ...}` answer itself."""
    try:
        decoded = as_object(cast(object, json.loads(text)))
    except ValueError:
        return False
    return decoded.get("text") == RESOURCE_TEXT and "uri" in decoded


def _reached(server: AppServer, path: str) -> str:
    """Whether the shop got to the app at all, for a failure that might be either side."""
    seen = server.state.seen(path)
    if not seen:
        return f" (the app server never received POST {path})"
    if not all(entry.verified for entry in seen):
        return f" (POST {path} reached the app with a signature that did not verify)"
    return ""


def verify_app_capabilities(
    rep: Reporter,
    endpoint: Endpoint,
    *,
    enabled: bool,
    public_host: str = "127.0.0.1",
    bind: str = "127.0.0.1",
    connect: Callable[[], AdminApi] | None = None,
) -> None:
    """Install the fixture app, run the four checks, remove it again.

    `public_host` is the host the shop reaches this process by, `bind` the
    address the app server listens on. They are the same in CI, where the shop
    runs on the runner; a containerised lane needs the host's address as seen
    from the container.
    """
    rep.section(SECTION)
    if not enabled:
        rep.check_skip(ALL_CHECKS, NEEDS_FLAG)
        return
    try:
        api = connect() if connect else AdminApi(endpoint.base_url, SW_ACCESS_KEY, SW_SECRET_ACCESS_KEY)
    except ProvisioningFailed as exc:
        rep.check_fail(INSTALL_CHECK, str(exc))
        return
    with AppServer(bind) as server:
        rep.info(f"Fixture app server: {server.base_url(public_host)} (listening on {bind})")
        try:
            try:
                install(api, render_app(server.base_url(public_host), server.state.manifest_secret))
            except ProvisioningFailed as exc:
                rep.check_fail(INSTALL_CHECK, f"{exc}{_reached(server, '/register')}")
                return
            if not any(entry.verified for entry in server.state.seen("/register")):
                rep.check_fail(INSTALL_CHECK, "the app installed without a verified registration request")
                return
            rep.check_pass(INSTALL_CHECK)
            session, _ = mcp_init(endpoint=endpoint)
            check_tool(rep, session, endpoint, server)
            try:
                check_listed(rep, session, endpoint)
            except RuntimeError as exc:
                rep.check_fail(LISTED_CHECK, str(exc))
            check_prompt(rep, session, endpoint, server)
            check_resource(rep, session, endpoint, server)
        finally:
            try:
                remove(api)
                rep.check_pass(CLEANUP_CHECK)
            except ProvisioningFailed as exc:
                rep.check_fail(CLEANUP_CHECK, f"{exc}. Remove {APP_NAME} by hand before the LLM eval runs.")
