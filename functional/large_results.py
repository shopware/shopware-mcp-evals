"""A result too large to send inline, and who may read it back.

Above 100 KB (`McpToolResponse::MAX_RESPONSE_SIZE`) a tool's result is stored
and the reply carries a pointer instead. shopware/shopware#20951 made that
pointer a signed token, `<id>.<expiry>.<signature>`, bound to the principal that
stored it rather than to the MCP session. Where the pointer travels depends on
the revision only (shopware/shopware#21844):

  before 2025-06-18 `_meta.resourceUri` and `_meta.expiresAt` in the legacy
                    text, and no `resource_link` block
  2025-06-18 on     a `resource_link` block and nowhere else: the text has no
                    `resourceUri`, and the expiry is `shopware/expiresAt` in
                    the result `_meta`

and `resources/read` on that URI answers the same principal on any later
session, and nobody else.

The probe is the PR's own reproducer. On the demo data it measured 346 KB, so it
needs no seeding; if the data ever shrinks below the threshold, the check says
the result came back inline rather than passing on a pointer it never saw.
"""

import json
import re
from collections.abc import Sequence
from typing import cast

from eval.result_schema import JsonObject, McpResponse, as_list, as_object
from functional.principals import AdminApi, ProvisioningFailed, another_principal
from functional.reporting import Reporter
from mcp_client import Endpoint, admin_endpoint, mcp_call, mcp_init, mcp_read_resource
from mcp_result import META_PREFIX, TOOL_RESULT_URI_PREFIX, ResultLink, ToolOutcome, read_result

OFFLOAD_TOOL = "shopware-entity-search"
OFFLOAD_ARGS: JsonObject = {"entity": "country", "associations": {"states": {}}, "limit": 500}

# Below and from the boundary; the pointer's shape changes there and nowhere else.
OFFLOAD_REVISIONS = ("2024-11-05", "2025-06-18")
LINK_SINCE = "2025-06-18"

PREDATES_SIGNED = (
    "the server predates shopware/shopware#20951: its pointer is a plain id bound to the MCP session, "
    "not a signed token bound to the principal"
)
NEEDS_PROVISIONING = "needs --provision-principals: it creates a second integration to try the pointer with"
NO_STORE_OFFLOAD = (
    "no Store tool can be made to offload: the UCP tools do not go through core's McpToolResponse, "
    "and shopware-store-api-context answers far below 100 KB"
)
NOT_TESTED_EXPIRY = "the one-hour expiry is time-bound and covered by core's unit tests"

# `McpException::toolResultNotFound`. Matched on its own words, because a
# JSON-RPC "Method not found" must not pass as a refusal.
_NOT_FOUND = re.compile(r"tool result .* not found", re.IGNORECASE)


def pointer(outcome: ToolOutcome) -> ResultLink | None:
    """The stored result's pointer, preferring the `resource_link` block. A
    link of the tool's own, which the renderer keeps next to it, is not one."""
    return next((link for link in outcome.links if link.uri.startswith(TOOL_RESULT_URI_PREFIX)), None)


def is_signed(uri: str) -> bool:
    """`<32 hex>.<unix time>.<signature>`; a plain id is the pre-#20951 pointer."""
    parts = uri.removeprefix(TOOL_RESULT_URI_PREFIX).split(".")
    return len(parts) == 3 and re.fullmatch(r"[0-9a-f]{32}", parts[0]) is not None and parts[1].isdigit()


def tampered(uri: str) -> str:
    """The same pointer with its expiry an hour later and the signature kept: a
    server that does not sign the expiry would accept it."""
    token = uri.removeprefix(TOOL_RESULT_URI_PREFIX)
    stored, expiry, signature = token.split(".")
    return f"{TOOL_RESULT_URI_PREFIX}{stored}.{int(expiry) + 3600}.{signature}"


def offload_problems(outcome: ToolOutcome, revision: str) -> list[str]:
    """Everything wrong with an offloaded reply on one revision; [] for nothing."""
    if not outcome.ok:
        return [f"the call failed: {outcome.failure}"]
    link = pointer(outcome)
    if link is None:
        return [f"the result came back inline ({len(outcome.text)} bytes): the probe no longer exceeds 100 KB"]

    problems: list[str] = []
    if not is_signed(link.uri):
        problems.append(f"the pointer is not a signed token: {link.uri}")
    if outcome.data is not None:
        problems.append("data was sent inline next to the pointer")
    text_uri = outcome.meta.get("resourceUri")
    # `pointer` prefers the block, so the pointer came from the text only
    # when there is no block.
    has_block = link.source == "resource_link"
    if revision >= LINK_SINCE:
        # The block is the pointer. A copy in the text is what a client below
        # the boundary reads, so finding one here means the legacy rendering.
        if not has_block:
            problems.append(f"no resource_link block on {revision}")
        if text_uri is not None:
            problems.append(f"resourceUri in the text on {revision}, where the resource_link block is the pointer")
        if META_PREFIX + "expiresAt" not in outcome.result_meta:
            problems.append(f"no shopware/expiresAt in the result _meta on {revision}")
    else:
        if text_uri is None:
            problems.append("no resourceUri next to the pointer in the text")
        if outcome.meta.get("expiresAt") is None:
            problems.append("no expiresAt next to the pointer in the text")
        if has_block:
            problems.append(f"resource_link block sent to a {revision} client, which predates it")
    return problems


def readable_problem(resp: McpResponse, revision: str) -> str | None:
    """None when resources/read returned the result stored on `revision`, else
    what it returned.

    The stored form follows the revision that stored it
    (`McpToolResultStoredFormat`): the legacy envelope below `LINK_SINCE`, the
    plain data from it on, which for the probe is the list of rows itself.
    """
    if "error" in resp:
        return f"refused: {as_object(resp.get('error')).get('message')}"
    contents = as_list(as_object(resp.get("result")).get("contents"))
    text = as_object(contents[0]).get("text") if contents else None
    if not isinstance(text, str):
        return "no text in the resource contents"
    try:
        stored = cast(object, json.loads(text))
    except json.JSONDecodeError:
        return f"the stored result is not JSON: {text[:80]}"
    if revision >= LINK_SINCE:
        if "success" in as_object(stored):
            return f"the stored result is the legacy envelope, stored on {revision}, which stores the plain data"
        rows = as_list(stored)
    else:
        rows = as_list(as_object(stored).get("data"))
    return None if rows else "the stored result carries no rows"


def refused_problem(resp: McpResponse) -> str | None:
    """None when resources/read refused with the tool-result not-found error."""
    if "error" not in resp:
        return "the stored result was returned"
    message = str(as_object(resp.get("error")).get("message") or "")
    return None if _NOT_FOUND.search(message) else f"refused, but not as not-found: {message[:200]}"


def _report(rep: Reporter, label: str, problem: str | None) -> None:
    if problem:
        rep.check_fail(label, problem)
    else:
        rep.check_pass(label)


def verify_large_results(
    rep: Reporter,
    endpoint: Endpoint,
    provision: bool,
    revisions: Sequence[str] = OFFLOAD_REVISIONS,
) -> None:
    """Offload one result per revision, then try its pointer from three callers."""
    rep.section("Large results and signed pointers (shopware/shopware#20951)")
    uri: str | None = None
    stored_on = endpoint.protocol_version
    for revision in revisions:
        pinned = endpoint.with_protocol(revision)
        try:
            session, _ = mcp_init(endpoint=pinned)
        except RuntimeError as exc:
            rep.check_fail(f"large result {revision}: opens a session", str(exc)[:200])
            continue
        outcome = read_result(mcp_call(session, OFFLOAD_TOOL, OFFLOAD_ARGS, endpoint=pinned))
        link = pointer(outcome)
        if revision == revisions[0] and link is not None and not is_signed(link.uri):
            rep.check_skip("large results", PREDATES_SIGNED)
            return
        problems = offload_problems(outcome, revision)
        _report(rep, f"large result {revision}: stored behind a signed pointer", "; ".join(problems))
        if link is not None and not problems:
            uri, stored_on = link.uri, revision

    if uri is not None:
        verify_pointer_binding(rep, endpoint, uri, provision, stored_on)
    rep.check_skip("large result pointer: expires after an hour", NOT_TESTED_EXPIRY)


def verify_pointer_binding(
    rep: Reporter, endpoint: Endpoint, uri: str, provision: bool, stored_on: str = LINK_SINCE
) -> None:
    """The pointer is the principal's, not the session's. `stored_on` is the
    revision whose call stored it, which decides the stored form."""
    try:
        fresh, _ = mcp_init(endpoint=endpoint)
    except RuntimeError as exc:
        rep.check_fail("large result pointer: opens a session to read it back", str(exc)[:200])
        return
    _report(
        rep,
        "large result pointer: the same principal reads it on a new session",
        readable_problem(mcp_read_resource(fresh, uri, endpoint), stored_on),
    )
    _report(
        rep,
        "large result pointer: a later expiry under the same signature is refused",
        refused_problem(mcp_read_resource(fresh, tampered(uri), endpoint)),
    )

    label = "large result pointer: another principal is refused"
    if not provision:
        rep.check_skip(label, NEEDS_PROVISIONING)
        return
    try:
        api = AdminApi(
            endpoint.base_url, endpoint.auth_headers["sw-access-key"], endpoint.auth_headers["sw-secret-access-key"]
        )
        with another_principal(api) as other:
            theirs = admin_endpoint(other.access_key, other.secret_key, base_url=endpoint.base_url).with_protocol(
                endpoint.protocol_version
            )
            session, _ = mcp_init(endpoint=theirs)
            _report(rep, label, refused_problem(mcp_read_resource(session, uri, theirs)))
    except ProvisioningFailed as exc:
        rep.check_fail(label, f"could not provision the second principal: {exc}")
