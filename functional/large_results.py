"""A result too large to send inline, and who may read it back.

Above about 100 KB of data a tool's result is stored and the reply carries a
pointer instead. shopware/shopware#20951 made that pointer a signed token,
`<id>.<expiry>.<signature>`, bound to the principal that stored it rather than
to the MCP session, and `resources/read` on it answers the same principal on
any later session, and nobody else.

How the pointer travels depends on the server generation:

  since #21844      the revision picks the format (`mcp_result.format_for`):
    before 2025-06-18  the legacy text, `_meta.resourceUri` and
                       `_meta.expiresAt` in it, no `resource_link`
    2025-06-18 on      the MCP format: a `resource_link` block, the offload
                       note as the summary, no `resourceUri` anywhere and no
                       `structuredContent`
    resources/read     the stored content follows the revision of the call
                       that stored it, not the reader's: the plain data from
                       2025-06-18 on, the legacy envelope before
  before #21844     one format on every revision, `_meta.resourceUri` in the
                    text everywhere, and from 2025-06-18 a `resource_link`
                    block with the same URI on top; stored as the envelope

`format_verdict` over both revisions' replies tells the generations apart, so
an older server (the pinned `shopware.sha`) keeps its own rules instead of
failing the newer ones.

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
from mcp_result import (
    META_PREFIX,
    TOOL_RESULT_URI_PREFIX,
    ResultFormat,
    ResultLink,
    ToolOutcome,
    format_for,
    format_verdict,
    read_result,
)

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
    """The stored result's pointer, preferring the `resource_link` block."""
    return outcome.links[0] if outcome.links else None


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


def offload_problems(outcome: ToolOutcome, revision: str, *, by_revision: bool = True) -> list[str]:
    """Everything wrong with an offloaded reply on one revision; [] for nothing.

    `by_revision`: the server picks the format by revision (shopware/shopware#21844).
    False keeps the rules of a server from before it.
    """
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
    if by_revision:
        era = legacy_era_problems if format_for(revision) == "legacy" else spec_era_problems
        return problems + era(outcome, link, revision)
    return problems + predating_problems(outcome, link, revision)


def legacy_era_problems(outcome: ToolOutcome, link: ResultLink, revision: str) -> list[str]:
    """Before 2025-06-18 since #21844: the deprecated legacy text, unchanged."""
    problems: list[str] = []
    if outcome.shape != "legacy":
        problems.append(f"the text is the {outcome.shape} format, a {revision} client gets the legacy envelope")
    if outcome.meta.get("resourceUri") != link.uri:
        problems.append("no resourceUri next to the pointer in the text")
    if outcome.meta.get("expiresAt") is None:
        problems.append("no expiresAt next to the pointer in the text")
    if any(lk.source == "resource_link" for lk in outcome.links):
        problems.append(f"resource_link block sent to a {revision} client, which predates it")
    if outcome.has_structured:
        problems.append(f"structuredContent sent to a {revision} client, which predates it")
    return problems


def spec_era_problems(outcome: ToolOutcome, link: ResultLink, revision: str) -> list[str]:
    """From 2025-06-18 since #21844: the MCP format links the stored result with
    a `resource_link` block and nothing else."""
    del link
    problems: list[str] = []
    if outcome.shape != "spec":
        problems.append(f"the text is the {outcome.shape} format, a {revision} client gets the MCP format")
    if not any(lk.source == "resource_link" for lk in outcome.links):
        problems.append(f"no resource_link block on {revision}")
    if "resourceUri" in outcome.meta or META_PREFIX + "resourceUri" in outcome.result_meta:
        problems.append("a resourceUri next to the resource_link: the MCP format links with the block only")
    if META_PREFIX + "expiresAt" not in outcome.result_meta:
        problems.append(f"no shopware/expiresAt in the result _meta on {revision}")
    if outcome.has_structured:
        problems.append("structuredContent next to the pointer: a linked result has no data to copy")
    return problems


def predating_problems(outcome: ToolOutcome, link: ResultLink, revision: str) -> list[str]:
    """A server from before #21844: one format on every revision, the pointer in
    the text everywhere, and a `resource_link` block on top from 2025-06-18."""
    problems: list[str] = []
    # The legacy text keeps the expiry in its envelope. The spec format (the
    # `v6.8.0.0` flag) does not: McpToolResultParser promotes it to the result's
    # expiry, which only the result `_meta` carries, on every revision.
    spec = outcome.shape == "spec"
    # The pointer itself stays in the text in both formats: the parser removes
    # only `note` and `expiresAt`. Without this, a reply carrying the link block
    # alone would pass on 2025-06-18 and later.
    text_uri = outcome.meta.get("resourceUri")
    if text_uri is None:
        problems.append("no resourceUri next to the pointer in the text")
    if not spec and outcome.meta.get("expiresAt") is None:
        problems.append("no expiresAt next to the pointer in the text")
    has_block = any(lk.source == "resource_link" for lk in outcome.links)
    if revision >= LINK_SINCE and not has_block:
        problems.append(f"no resource_link block on {revision}")
    if (spec or revision >= LINK_SINCE) and META_PREFIX + "expiresAt" not in outcome.result_meta:
        problems.append(f"no shopware/expiresAt in the result _meta on {revision}")
    if revision >= LINK_SINCE:
        if has_block and text_uri is not None and text_uri != link.uri:
            problems.append("the resource_link block and the text name different pointers")
    elif has_block:
        problems.append(f"resource_link block sent to a {revision} client, which predates it")
    return problems


def readable_problem(resp: McpResponse, stored: ResultFormat = "legacy") -> str | None:
    """None when resources/read returned the stored result in the format it was
    stored in, else what it returned. `spec`: the plain data, here the rows;
    `legacy`: the envelope with the rows in `data`."""
    if "error" in resp:
        return f"refused: {as_object(resp.get('error')).get('message')}"
    contents = as_list(as_object(resp.get("result")).get("contents"))
    text = as_object(contents[0]).get("text") if contents else None
    if not isinstance(text, str):
        return "no text in the resource contents"
    try:
        decoded = cast(object, json.loads(text))
    except json.JSONDecodeError:
        return f"the stored result is not JSON: {text[:80]}"
    envelope = "success" in as_object(decoded)
    rows: object
    if stored == "spec":
        if envelope:
            return "the stored result is the legacy envelope, expected the plain data"
        rows = decoded
    else:
        if not envelope:
            return "the stored result is not the legacy envelope"
        rows = as_object(decoded).get("data")
    if not as_list(rows):
        return "the stored result carries no rows"
    return None


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
    rep.section("Large results and signed pointers (shopware/shopware#20951, #21844)")
    replies: list[tuple[str, ToolOutcome]] = []
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
        replies.append((revision, outcome))

    # `mixed` keeps the per-revision rules, which then say what is wrong.
    by_revision = format_verdict(replies) != "one_format"
    pointers: list[tuple[str, str]] = []
    for revision, outcome in replies:
        problems = offload_problems(outcome, revision, by_revision=by_revision)
        _report(rep, f"large result {revision}: stored behind a signed pointer", "; ".join(problems))
        link = pointer(outcome)
        if link is not None and not problems:
            pointers.append((revision, link.uri))

    if pointers:
        verify_pointer_binding(rep, endpoint, pointers, provision, by_revision=by_revision)
    rep.check_skip("large result pointer: expires after an hour", NOT_TESTED_EXPIRY)


def verify_pointer_binding(
    rep: Reporter, endpoint: Endpoint, pointers: Sequence[tuple[str, str]], provision: bool, *, by_revision: bool
) -> None:
    """The pointer is the principal's, not the session's.

    Every pointer is read back on a session of the suite's own revision, so a
    2024-11-05 pointer is read by a newer client: what comes back follows the
    call that stored it. The refusals need one pointer, the newest.
    """
    try:
        fresh, _ = mcp_init(endpoint=endpoint)
    except RuntimeError as exc:
        rep.check_fail("large result pointer: opens a session to read it back", str(exc)[:200])
        return
    for revision, uri in pointers:
        stored: ResultFormat = format_for(revision) if by_revision else "legacy"
        _report(
            rep,
            f"large result {revision}: the same principal reads it on a new session ({stored} content)",
            readable_problem(mcp_read_resource(fresh, uri, endpoint), stored),
        )
    uri = pointers[-1][1]
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
