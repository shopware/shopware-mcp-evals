#!/usr/bin/env python3
"""Shopware MCP functional test runner (MCP Server v2: dynamic tool discovery).

Layer 1 of the eval harness. Opens an MCP session, verifies the v2 discovery
mechanics (default surface, toolset taxonomy, enable/isolation, tool-search),
then calls each tool with a minimal valid payload — mutating tools run with
dryRun=true. One runner covers both endpoints; the shared discovery checks are
parameterized by --endpoint.

  admin (default) : /api/_mcp,       auth via SW_ACCESS_KEY + SW_SECRET_ACCESS_KEY
  store           : /store-api/_mcp, auth via SW_SC_ACCESS_KEY

Requires (admin): SW_BASE_URL, SW_ACCESS_KEY, SW_SECRET_ACCESS_KEY
Requires (store): SW_BASE_URL, SW_SC_ACCESS_KEY

Usage:
  python -m functional.runner [--endpoint admin] [--skip-media-upload] [--skip-dev-tools]
  python -m functional.runner --endpoint store

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import requests

import lane
from eval.assertions import inband_error
from functional.checks import CORE_CHECKS, DEV_CHECKS, LOG_PROBE_TEXT, MERCHANT_CHECKS, ToolCheck
from functional.journeys import run_ucp_journey
from functional.reporting import Reporter
from mcp_client import (
    BASE,
    META_TOOLS,
    SW_ACCESS_KEY,
    SW_BASE_URL,
    SW_SC_ACCESS_KEY,
    SW_SECRET_ACCESS_KEY,
    Endpoint,
    enable_all_toolsets,
    enable_toolset,
    endpoint_by_name,
    mcp_call,
    mcp_init,
    mcp_result_text,
    mcp_tools_list_all,
    mcp_toolsets_list,
)

# typeId of the Storefront sales-channel type (used to find a storefront channel).
STOREFRONT_TYPE_ID = "8a243080f92e4c719546314b577cf82b"


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------
def _payload(resp: dict) -> dict:
    """Parse the JSON payload carried in a tools/call text content block."""
    try:
        parsed = json.loads(mcp_result_text(resp) or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _advertised(rep: Reporter, session: str, endpoint: Endpoint, label: str) -> list[str] | None:
    """Advertised tool names for a session, or None (after recording a fail)
    when pagination misbehaves."""
    try:
        return [t.get("name", "") for t in mcp_tools_list_all(session, endpoint=endpoint)]
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        rep.check_fail(label, str(exc))
        return None


def _first_field(session: str, endpoint: Endpoint, entity: str, field: str = "id", extra: dict | None = None) -> str:
    """First entity's field value via entity-search, retried a few times
    (a large payload can occasionally deliver a partial read). '' on failure."""
    for _ in range(3):
        args = {"entity": entity, "limit": 1}
        if extra:
            args.update(extra)
        items = _payload(mcp_call(session, "shopware-entity-search", args, endpoint=endpoint)).get("data", [])
        if items:
            value = items[0].get(field, "")
            if value:
                return value
    return ""


# ---------------------------------------------------------------------------
# Tool assertions
# ---------------------------------------------------------------------------
def assert_tool(
    rep: Reporter,
    session: str,
    endpoint: Endpoint,
    tool: str,
    args: dict,
    label: str | None = None,
    contains: str = "",
) -> dict:
    """Call a tool; pass only if it neither errored nor reported failure in band.

    Returns the parsed payload so a caller can thread an id into the next call —
    see functional/journeys.py, where each step's result is the next step's
    precondition.

    The in-band check is the load-bearing part. This used to pass on "no protocol
    error and some content", which is blind to the way UCP reports every failure:
    HTTP 200, no JSON-RPC error, and `{"success": false}` in the body. All 27
    admin checks were green over a mechanism that could not have seen a single
    Store failure. `eval/preflight.py` already had this right; this is the same
    `inband_error` and the same reasoning.
    """
    label = label or tool
    resp = mcp_call(session, tool, args, endpoint=endpoint)
    error = resp.get("error", {}).get("message", "")
    content = resp.get("result", {}).get("content", [])
    text = mcp_result_text(resp)
    if error:
        rep.tool_fail(tool, label, error)
    elif not content:
        rep.tool_fail(tool, label, "empty content in response")
    elif in_band := inband_error(text):
        rep.tool_fail(tool, label, in_band)
    elif contains and contains not in (text or ""):
        # The tool answered, and answered with the wrong thing. A reader pointed
        # at the wrong file returns an empty result and would otherwise pass.
        rep.tool_fail(tool, label, f"response did not contain {contains!r}")
    else:
        rep.tool_pass(tool, label, text[:120])
        return _payload(resp)
    return {}


def assert_tool_error(
    rep: Reporter, session: str, endpoint: Endpoint, tool: str, args: dict, expected: str, label: str
) -> None:
    """Call a tool and expect it to FAIL (protocol error, isError, or a
    {"success": false} payload), optionally containing `expected`."""
    resp = mcp_call(session, tool, args, endpoint=endpoint)
    msg = resp.get("error", {}).get("message", "")
    result = resp.get("result", {})
    text = mcp_result_text(resp)
    is_error = bool(msg) or bool(result.get("isError"))
    if not is_error:
        try:
            is_error = json.loads(text).get("success") is False
        except (ValueError, TypeError):
            pass
    if not is_error:
        rep.check_fail(label, "expected an error response, got: NO_ERROR")
    elif expected and expected not in (msg + text):
        rep.check_fail(label, f"expected an error response, got: WRONG_ERROR:{(msg + ' ' + text)[:120]}")
    else:
        rep.check_pass(label)


# ---------------------------------------------------------------------------
# Shared v2 discovery checks (both endpoints)
# ---------------------------------------------------------------------------
def verify_default_surface(rep: Reporter, session: str, endpoint: Endpoint) -> None:
    """A fresh session must advertise ONLY the three discovery meta-tools;
    every catalogue tool is deferred."""
    rep.section("v2: Default advertised surface")
    advertised = _advertised(rep, session, endpoint, "tools/list pagination")
    if advertised is None:
        return
    adv = set(advertised)
    for tool in sorted(META_TOOLS):
        if tool in adv:
            rep.check_pass(f"{tool} advertised by default")
        else:
            rep.check_fail(tool, "not in default tools/list")
    extras = adv - META_TOOLS
    if not extras:
        rep.check_pass("no deferred tools leak into the default surface")
    else:
        rep.check_fail("default surface", "unexpected tools advertised: " + " ".join(sorted(extras)))


def load_toolsets(session: str, endpoint: Endpoint) -> list[dict]:
    return mcp_toolsets_list(session, endpoint=endpoint)


def verify_tool_schemas(rep: Reporter, session: str, endpoint: Endpoint) -> None:
    """Every advertised tool must expose a JSON-Schema-valid inputSchema.

    Specifically `properties` must be an object. A parameterless tool is easy to
    get wrong here: PHP's json_encode renders an empty associative array as `[]`,
    and OpenAI rejects that with "[] is not of type 'object'" — so a single
    malformed tool breaks every request from an OpenAI-compatible client, not
    just calls to that tool.
    """
    rep.section("Tool schema conformance")
    try:
        # Check the whole catalogue, not just the default surface: a malformed
        # deferred tool breaks a client just as hard once its toolset is enabled.
        enable_all_toolsets(session, endpoint=endpoint)
        tools = mcp_tools_list_all(session, endpoint=endpoint)
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        rep.check_fail("tool schema conformance", str(exc))
        return

    malformed = _malformed_schemas(tools)
    if malformed:
        rep.check_fail("tool schema conformance", "; ".join(malformed))
    else:
        rep.check_pass(f"all {len(tools)} advertised tools expose an object-typed inputSchema.properties")

    # The check above lists tools *after* enabling every toolset, which drains the
    # tools/list_changed queue along the way, so the response comes back as plain
    # application/json. Enabling a single toolset and listing immediately is the
    # other shape: the pending notification rides along and the server answers
    # text/event-stream. Those are different code paths on the server, and the
    # SSE one shipped unnormalized while this check was passing — the eval caught
    # it and this did not. So assert the same invariant on that flow too.
    try:
        sse_session, _ = mcp_init(endpoint=endpoint)
        toolsets = mcp_toolsets_list(sse_session, endpoint=endpoint)
        if not toolsets:
            rep.skip("tool schema conformance (post-enable listing: no toolsets advertised)")
            return
        enable_toolset(sse_session, toolsets[0]["name"], endpoint=endpoint)
        after_enable = mcp_tools_list_all(sse_session, endpoint=endpoint)
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        rep.check_fail("tool schema conformance (post-enable listing)", str(exc))
        return

    malformed = _malformed_schemas(after_enable)
    if malformed:
        rep.check_fail("tool schema conformance (post-enable listing)", "; ".join(malformed))
    else:
        rep.check_pass(
            f"all {len(after_enable)} tools listed right after enabling '{toolsets[0]['name']}' "
            "expose an object-typed inputSchema.properties"
        )

    # Third path, and the one that actually shipped broken: shopware-tool-search
    # embeds whole tool definitions in its *result payload* rather than in
    # `result.tools`. A client surfaces those tools directly (the allowlist, not
    # advertising, is the call boundary), so their schemas reach the model the
    # same way — but they travel inside result.content[].text as a JSON string,
    # which server-side tools/list normalization does not reach. Both checks
    # above passed while this path served `"properties": []`.
    try:
        search_tools = _search_payload_tools(sse_session, endpoint)
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        rep.check_fail("tool schema conformance (tool-search payload)", str(exc))
        return

    if not search_tools:
        rep.skip("tool schema conformance (tool-search payload: tool-search returned no tools)")
        return

    malformed = _malformed_schemas(search_tools)
    if malformed:
        rep.check_fail("tool schema conformance (tool-search payload)", "; ".join(malformed))
    else:
        rep.check_pass(
            f"all {len(search_tools)} tool-search-surfaced tools expose an object-typed inputSchema.properties"
        )


def _search_payload_tools(session: str, endpoint: Endpoint) -> list[dict]:
    """Tool definitions embedded in shopware-tool-search results.

    Queried across several terms so parameterless tools — the ones that trip the
    empty-properties bug — are actually reached; a single query returns only its
    top matches.
    """
    seen: dict[str, dict] = {}
    for query in ("list", "skills", "search", "config", "order", "product"):
        payload = mcp_result_text(mcp_call(session, "shopware-tool-search", {"query": query}, endpoint=endpoint))
        try:
            data = json.loads(payload).get("data", [])
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        for row in data:
            tool = row.get("tool") if isinstance(row, dict) else None
            if isinstance(tool, dict) and tool.get("name"):
                seen[tool["name"]] = tool
    return list(seen.values())


def _malformed_schemas(tools: list[dict]) -> list[str]:
    """Names the tools whose inputSchema is not a JSON-Schema-valid object."""
    malformed = []
    for tool in tools:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            malformed.append(f"{tool.get('name')}: inputSchema is {type(schema).__name__}")
        elif not isinstance(schema.get("properties", {}), dict):
            malformed.append(
                f"{tool.get('name')}: properties is a {type(schema['properties']).__name__}, not an object"
            )
    return malformed


def verify_enable_and_isolation(
    rep: Reporter,
    endpoint: Endpoint,
    target_toolset: str,
    probe_tool: str,
    probe_label: str,
    check_default_persists: bool,
) -> None:
    """Enabling a toolset grows the session's advertised list, emits
    _meta.listChanged, and does not leak into other sessions."""
    session_a, _ = mcp_init(endpoint=endpoint)
    if not session_a:
        rep.check_fail("discovery session A", "mcp_init failed")
        return

    payload = _payload(enable_toolset(session_a, target_toolset, endpoint=endpoint))
    if payload.get("success") and payload.get("_meta", {}).get("listChanged"):
        rep.check_pass(f"toolset-enable ({target_toolset}) succeeds with _meta.listChanged=true")
    else:
        rep.check_fail(f"toolset-enable ({target_toolset})", "no success/listChanged in response")

    advertised_a = _advertised(rep, session_a, endpoint, "enable grows tools/list") or []
    if probe_tool in advertised_a:
        rep.check_pass(f"{probe_label} advertised after enabling {target_toolset}")
    else:
        rep.check_fail("enable grows tools/list", f"{probe_tool} still not advertised")
    if check_default_persists:
        if "shopware-tool-search" in advertised_a:
            rep.check_pass("default tools still advertised after enable")
        else:
            rep.check_fail("default tools after enable", "shopware-tool-search missing")

    session_b, _ = mcp_init(endpoint=endpoint)
    if not session_b:
        rep.check_fail("discovery session B", "mcp_init failed")
        return
    advertised_b = _advertised(rep, session_b, endpoint, "session isolation") or []
    if probe_tool in advertised_b:
        rep.check_fail("session isolation", "session B sees toolset enabled in session A")
    else:
        rep.check_pass("toolset enablement does not leak across sessions")


def run_search(session: str, endpoint: Endpoint, query: str, max_results: int) -> dict:
    return _payload(
        mcp_call(session, "shopware-tool-search", {"query": query, "maxResults": max_results}, endpoint=endpoint)
    )


# ---------------------------------------------------------------------------
# Admin endpoint
# ---------------------------------------------------------------------------
def verify_admin_toolsets(rep: Reporter, session: str, endpoint: Endpoint) -> tuple[str, list[dict]]:
    """Toolset taxonomy: complete metadata, enabled=false on a fresh session,
    and a toolset that contains shopware-entity-read. Returns
    (entity_toolset, toolsets)."""
    rep.section("v2: Toolset taxonomy")
    toolsets = load_toolsets(session, endpoint)

    required = {"name", "title", "description", "tools", "enabled"}
    problems: list[str] = []
    union: set[str] = set()
    entity_toolset = ""
    for ts in toolsets:
        missing = required - set(ts.keys())
        if missing:
            problems.append(f"{ts.get('name', '?')} missing fields: {sorted(missing)}")
        if ts.get("enabled") is not False:
            problems.append(f"{ts.get('name', '?')} enabled != false on fresh session")
        union.update(ts.get("tools", []))
        if "shopware-entity-read" in ts.get("tools", []):
            entity_toolset = ts["name"]

    if len(toolsets) >= 8:
        rep.check_pass(f"toolsets-list returns {len(toolsets)} toolsets (>= 8)")
    else:
        rep.check_fail("toolsets-list", f"only {len(toolsets)} toolsets returned")
    if not problems:
        rep.check_pass("every toolset has name/title/description/tools and enabled=false")
    else:
        rep.check_fail("toolset metadata", "; ".join(problems))
    if len(union) >= 18:
        rep.check_pass(f"toolsets cover {len(union)} deferred tools")
    else:
        rep.check_fail("toolset coverage", f"union of toolset tools is only {len(union)}")
    if entity_toolset:
        rep.check_pass(f"found toolset containing shopware-entity-read: {entity_toolset}")
    else:
        rep.check_fail("entity toolset", "no toolset contains shopware-entity-read")

    return entity_toolset, toolsets


def verify_admin_discovery(rep: Reporter, endpoint: Endpoint, entity_toolset: str, toolsets: list[dict]) -> None:
    """Admin discovery mechanics: enable/isolation, deferred-callable,
    tool-search behavior, unknown-toolset rejection, activate-all completeness."""
    rep.section("v2: Discovery mechanics")

    if entity_toolset:
        verify_enable_and_isolation(
            rep,
            endpoint,
            entity_toolset,
            probe_tool="shopware-entity-read",
            probe_label="shopware-entity-read",
            check_default_persists=True,
        )
    else:
        rep.skip("toolset-enable test (no entity toolset found)")

    session, _ = mcp_init(endpoint=endpoint)
    if not session:
        rep.check_fail("discovery probe session", "mcp_init failed")
        return

    # deferred tools stay directly callable (allowlist is the call boundary)
    assert_tool(
        rep,
        session,
        endpoint,
        "shopware-system-config-read",
        {"key": "core.basicInformation"},
        "deferred tool callable without enable (system-config-read)",
    )

    # tool-search: ranked results spanning deferred tools
    search = run_search(session, endpoint, "upload an image file", 5)
    results = search.get("data", [])
    meta = search.get("_meta", {})
    problems: list[str] = []
    if not search.get("success"):
        problems.append("success != true")
    if not results:
        problems.append("no results")
    if any(not all(k in r for k in ("tool", "score", "matchedIn")) for r in results):
        problems.append("result missing tool/score/matchedIn")
    names = [r.get("tool", {}).get("name", "") for r in results]
    if "shopware-media-upload" not in names:
        problems.append(f"shopware-media-upload not in results: {names}")
    if "query" not in meta or "totalCandidates" not in meta:
        problems.append("_meta missing query/totalCandidates")
    if not problems:
        rep.check_pass("tool-search finds deferred shopware-media-upload with score/matchedIn")
    else:
        rep.check_fail("tool-search (upload an image file)", "; ".join(problems))

    # tool-search caps maxResults at 20
    cap_count = len(run_search(session, endpoint, "shopware", 50).get("data", []))
    if 1 <= cap_count <= 20:
        rep.check_pass(f"tool-search caps maxResults at 20 (got {cap_count})")
    else:
        rep.check_fail("tool-search maxResults cap", f"expected 1..20 results, got {cap_count}")

    # unknown toolset is rejected
    assert_tool_error(
        rep,
        session,
        endpoint,
        "shopware-toolset-enable",
        {"toolset": "does-not-exist"},
        "Unknown",
        "toolset-enable rejects unknown toolset",
    )

    # activate ALL toolsets: every catalogue tool must become reachable
    session_all, _ = mcp_init(endpoint=endpoint)
    if not session_all:
        rep.check_fail("activate-all session", "mcp_init failed")
        return
    names_all = [ts["name"] for ts in toolsets]
    enabled_count = sum(
        1 for name in names_all if _payload(enable_toolset(session_all, name, endpoint=endpoint)).get("success")
    )
    if enabled_count == len(names_all):
        rep.check_pass(f"activated all {len(names_all)} toolsets in one session")
    else:
        rep.check_fail("activate all toolsets", f"only {enabled_count}/{len(names_all)} enable calls succeeded")

    union_tools = {tool for ts in toolsets for tool in ts.get("tools", [])}
    expected_full = META_TOOLS | union_tools
    advertised_full = _advertised(rep, session_all, endpoint, "tools/list pagination after activate-all")
    if advertised_full is None:
        return
    advertised_set = set(advertised_full)
    missing = expected_full - advertised_set
    extra = advertised_set - expected_full
    if not missing and not extra:
        rep.check_pass(
            f"all tools reachable: {len(advertised_set)}/{len(expected_full)} advertised after "
            f"activating every toolset (3 meta + {len(union_tools)} toolset tools)"
        )
    else:
        rep.check_fail(
            "activate-all completeness",
            f"expected {len(expected_full)}, got {len(advertised_set)}; "
            f"missing: {' '.join(sorted(missing)) or 'none'}; "
            f"extra: {' '.join(sorted(extra)) or 'none'}",
        )


def run_checks(rep: Reporter, session: str, endpoint: Endpoint, checks: tuple[ToolCheck, ...], ctx: dict) -> None:
    """Run a table of checks, skipping any whose prerequisites are missing."""
    for check in checks:
        reason = check.blocked_by(ctx)
        if reason:
            rep.skip(check.skip_label(reason))
        else:
            assert_tool(
                rep,
                session,
                endpoint,
                check.tool,
                check.args(ctx),
                check.label(ctx),
                # Only assert on content the lane actually seeded. Elsewhere
                # there is nothing known to look for, and demanding it would
                # fail every shop this suite did not build.
                contains=check.contains if ctx.get("log_probe", True) else "",
            )


def gather_context(session: str, endpoint: Endpoint, args: argparse.Namespace) -> dict:
    """The live ids the check payloads need.

    Every one is optional: an empty shop yields no product to read and no sales
    channel to price against, and the affected checks skip rather than fail.
    """
    return {
        "product_id": _first_field(session, endpoint, "product"),
        "order_id": _first_field(session, endpoint, "order"),
        "customer_email": _first_field(session, endpoint, "customer", field="email"),
        "customer_id": _first_field(session, endpoint, "customer"),
        "sales_channel_id": _first_field(
            session,
            endpoint,
            "sales_channel",
            extra={
                "criteria": json.dumps({"filter": [{"type": "equals", "field": "typeId", "value": STOREFRONT_TYPE_ID}]})
            },
        ),
        # Inverted so the check table can treat it like any other prerequisite.
        "media_upload_enabled": not args.skip_media_upload,
    }


def sellable_products(session: str, endpoint: Endpoint, sales_channel_id: str) -> list[str]:
    """Storefront-sellable product candidates. See lane.sellable_products.

    Wrapped rather than imported bare so the eval and functional suites cannot
    drift apart on what "sellable" means again — eval/ was still adding the
    first search hit and trusting `success: true` long after this suite learned
    that answers 200 with an empty cart.
    """
    return lane.sellable_products(session, endpoint, sales_channel_id)


def newest_log_file(session: str, endpoint: Endpoint) -> str:
    """A log file the dev-tools log readers can actually open.

    `file` defaults to an empty string, which is never a real filename, so both
    log checks failed with "Log file not found" on every instance. The tool lists
    the valid values in that very error, so ask it rather than guessing a name
    that depends on the date and the APP_ENV.
    """
    text = mcp_result_text(
        mcp_call(session, "swag-dev-tools-log-search", {"query": "x", "limit": 1}, endpoint=endpoint)
    )
    marker = "Available files:"
    if marker not in (text or ""):
        return ""
    listed = text.split(marker, 1)[1].strip().rstrip('"}').split(",")
    files = [f.strip().strip('"') for f in listed if f.strip()]
    # max(), not files[-1]. A dated name sorts chronologically so the newest wins
    # either way IF the server returns them ordered — and nothing promises that.
    # Taking the last element made a correct-looking result depend on an
    # undocumented detail of somebody else's response.
    return max(files) if files else ""


def find_log_probe(session: str, endpoint: Endpoint, files_hint: str) -> tuple[str, bool]:
    """The log file holding the line the lane seeded, and whether it was found.

    Returns (file, seeded). A seeded file lets the readers be asserted properly:
    they have to return a line we know is there, which "the tool answered" does
    not establish — a reader pointed at the wrong file returns an empty result
    and passes.

    Falls back to the newest real log where no lane seeded one, because someone
    else's shop has no probe and demanding it would fail every instance this
    suite did not build.
    """
    probe = mcp_result_text(
        mcp_call(
            session,
            "swag-dev-tools-log-search",
            {"query": LOG_PROBE_TEXT, "limit": 1, "file": files_hint},
            endpoint=endpoint,
        )
    )
    if LOG_PROBE_TEXT in (probe or ""):
        return files_hint, True
    return files_hint, False


def first_skill_name(session: str, endpoint: Endpoint) -> str:
    """A real skill name for load-skill, taken from list-skills.

    The payload has been both `{"skills": [...]}` and a bare list, and its items
    both dicts and strings, so all four shapes are tolerated.
    """
    data = _payload(mcp_call(session, "swag-dev-tools-list-skills", {}, endpoint=endpoint)).get("data", {})
    skills = data.get("skills", data) if isinstance(data, dict) else data
    if isinstance(skills, list) and skills:
        first = skills[0]
        return first.get("name", "") if isinstance(first, dict) else str(first)
    return ""


def create_cart_token(session: str, endpoint: Endpoint, sales_channel_id: str, product_ids: list[str]) -> str:
    """A cart with something in it, for merchant-cart-checkout to check out.

    This creates one *in addition to* the cart the merchant-cart-manage check
    creates: that check has to make its own call to be a real assertion, and
    reusing this token would make the two indistinguishable on failure.

    Returns "" when nothing could be added. The checkout check declares
    `cart_token` a precondition, so that reads as a SKIP naming the missing
    data — the honest verdict for a lane with no sellable product, and not the
    same claim as "checkout is broken".
    """
    token, _line_item_id = lane.create_cart(session, endpoint, sales_channel_id, product_ids)
    return token


def run_admin_tools(rep: Reporter, session: str, endpoint: Endpoint, args: argparse.Namespace) -> None:
    """Per-tool assertions on a session with NO toolsets enabled — each call to
    a deferred tool doubles as a direct-callability assertion."""
    ctx = gather_context(session, endpoint, args)

    rep.section("Core tools")
    run_checks(rep, session, endpoint, CORE_CHECKS, ctx)

    rep.section("Merchant tools")
    # Storefront-visible products, not just any product row. entity-search
    # returns products that are inactive, out of stock, or not in this channel,
    # and adding one answers `success: true` with an empty cart — so the checkout
    # check failed with "Cart is empty" while looking like a checkout bug.
    # Several candidates, because the storefront search does not filter for
    # sellability either: create_cart_token adds them until the cart reads back
    # non-empty, and returns "" if none does.
    ctx["cart_product_ids"] = sellable_products(session, endpoint, ctx["sales_channel_id"])
    ctx["cart_token"] = create_cart_token(session, endpoint, ctx["sales_channel_id"], ctx["cart_product_ids"])
    run_checks(rep, session, endpoint, MERCHANT_CHECKS, ctx)

    rep.section("Dev tools")
    if args.skip_dev_tools:
        rep.skip("dev tools (--skip-dev-tools)")
        return
    ctx["log_file"] = newest_log_file(session, endpoint)
    ctx["log_file"], ctx["log_probe"] = find_log_probe(session, endpoint, ctx["log_file"])
    # Needs the tool it is named after to have run, so it cannot be part of ctx.
    ctx["skill_name"] = first_skill_name(session, endpoint)
    run_checks(rep, session, endpoint, DEV_CHECKS, ctx)


def run_admin(rep: Reporter, endpoint: Endpoint, args: argparse.Namespace, session: str) -> None:
    verify_default_surface(rep, session, endpoint)
    entity_toolset, toolsets = verify_admin_toolsets(rep, session, endpoint)
    verify_admin_discovery(rep, endpoint, entity_toolset, toolsets)
    schema_session, _ = mcp_init(endpoint=endpoint)
    verify_tool_schemas(rep, schema_session, endpoint)
    run_admin_tools(rep, session, endpoint, args)


# ---------------------------------------------------------------------------
# Store API endpoint
# ---------------------------------------------------------------------------
def run_store(rep: Reporter, endpoint: Endpoint, session: str, allow_mutations: bool = False) -> None:
    """The Store API endpoint: v2 discovery mechanics, then the buyer journey.

    It used to stop before calling any UCP tool, on the grounds that they need
    provisioned state. They do — which is why the journey provisions it, rather
    than leaving thirteen tools untested and their fixtures graded on the tool
    name alone."""
    verify_default_surface(rep, session, endpoint)

    # --- toolset taxonomy ---
    rep.section("v2: Toolset taxonomy")
    toolsets = load_toolsets(session, endpoint)
    union: set[str] = set()
    ucp_toolset = ""
    ucp_probe = ""
    for ts in toolsets:
        union.update(ts.get("tools", []))
        # The UCP tools are spread over several granular toolsets (cart, checkout,
        # catalog, ...). Take the first one and probe a tool that actually belongs
        # to it — a hardcoded probe would break whenever the taxonomy is resliced.
        ucp_tools = sorted(n for n in ts.get("tools", []) if n.startswith("shopware-ucp-"))
        if ucp_tools and not ucp_toolset:
            ucp_toolset = ts["name"]
            ucp_probe = ucp_tools[0]
    if len(toolsets) >= 2:
        rep.check_pass(f"toolsets-list returns {len(toolsets)} toolsets (>= 2)")
    else:
        rep.check_fail("toolsets-list", f"only {len(toolsets)}")
    if ucp_toolset:
        rep.check_pass(f"found UCP toolset: {ucp_toolset}")
    else:
        rep.check_fail("UCP toolset", "no toolset holds shopware-ucp-* tools")
    if len(union) >= 13:
        rep.check_pass(f"toolsets cover {len(union)} deferred store tools")
    else:
        rep.check_fail("toolset coverage", f"only {len(union)}")

    # --- enable grows the list + listChanged; session isolation ---
    rep.section("v2: Discovery mechanics")
    if ucp_toolset:
        verify_enable_and_isolation(
            rep,
            endpoint,
            ucp_toolset,
            probe_tool=ucp_probe,
            probe_label=ucp_probe,
            check_default_persists=False,
        )
    else:
        rep.skip("enable/isolation (no UCP toolset found)")

    # --- store-api-context: deferred but directly callable ---
    rep.section("Store context & search")
    ctx_session, _ = mcp_init(endpoint=endpoint)
    ctx = _payload(mcp_call(ctx_session or session, "shopware-store-api-context", {}, endpoint=endpoint))
    data = ctx.get("data", {})
    if ctx.get("success") and data.get("salesChannelId") and data.get("token"):
        rep.check_pass("shopware-store-api-context (deferred, callable, returns channel+token)")
    else:
        rep.check_fail("shopware-store-api-context", "missing salesChannelId/token or errored")

    # --- tool-search finds a deferred UCP tool ---
    search = run_search(session, endpoint, "add items to a shopping cart", 5)
    names = [r.get("tool", {}).get("name", "") for r in search.get("data", [])]
    if search.get("success") and any(name.startswith("shopware-ucp-cart") for name in names):
        rep.check_pass("shopware-tool-search finds a deferred UCP cart tool")
    else:
        rep.check_fail("shopware-tool-search", "no UCP cart tool in results")

    # --- the buyer journey ---
    #
    # This is where the UCP tools are actually exercised. Everything above tests
    # the discovery layer around them; only the journey tests the tools, because
    # they are one flow and an isolated call to any of them mostly proves how the
    # server words "not found".
    rep.section("UCP buyer journey")
    journey_session, _ = mcp_init(endpoint=endpoint)
    enable_all_toolsets(journey_session, endpoint=endpoint)
    run_ucp_journey(rep, journey_session, endpoint, allow_mutations=allow_mutations)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def require(name: str, value: str) -> None:
    if not value:
        sys.exit(f"ERROR: {name} is required")


def main() -> int:
    parser = argparse.ArgumentParser(description="Shopware MCP functional test runner")
    parser.add_argument("--endpoint", choices=["admin", "store"], default="admin")
    parser.add_argument("--skip-media-upload", action="store_true")
    parser.add_argument("--skip-dev-tools", action="store_true")
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help=(
            "Let the UCP buyer journey commit: it creates a cart and a checkout and PLACES A REAL "
            "ORDER. Only for a disposable lane (CI, a local trunk lane) — never a shop you care "
            "about. Without it the journey is skipped and says so."
        ),
    )
    args = parser.parse_args()

    require("SW_BASE_URL", SW_BASE_URL)
    endpoint = endpoint_by_name(args.endpoint)
    if args.endpoint == "admin":
        require("SW_ACCESS_KEY", SW_ACCESS_KEY)
        require("SW_SECRET_ACCESS_KEY", SW_SECRET_ACCESS_KEY)
    else:
        require("SW_SC_ACCESS_KEY (sales-channel access key)", SW_SC_ACCESS_KEY)

    rep = Reporter(SW_BASE_URL)
    rep.banner(f"Shopware MCP Functional Tests — {args.endpoint} (v2 discovery)")
    rep.info(f"Endpoint: {endpoint.url}\n")
    rep.info("Initializing MCP session...")
    try:
        session, _ = mcp_init(endpoint=endpoint)
    except Exception as exc:  # noqa: BLE001 — surface any connection/auth failure clearly
        print(f"ERROR: Failed to initialize MCP session: {exc}")
        return 1
    if not session:
        print("ERROR: Failed to initialize MCP session. Check credentials.")
        return 1
    rep.info(f"Session: {session}")

    try:
        if args.endpoint == "admin":
            run_admin(rep, endpoint, args, session)
        else:
            run_store(rep, endpoint, session, allow_mutations=args.allow_mutations)
    except requests.exceptions.RequestException as exc:
        # A transport failure that survived the client's throttle retries — record
        # it and still emit a summary + report rather than crashing mid-suite.
        rep.check_fail("transport", f"request failed, suite aborted early: {exc}")

    rep.summary()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rep.write_report(BASE / "results" / f"functional-{args.endpoint}-{timestamp}.json")
    # Stable filename: the eval job consumes this as an artifact, so it cannot
    # be timestamped like the report.
    rep.write_health(BASE / "results" / f"tool-health-{args.endpoint}.json")
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
