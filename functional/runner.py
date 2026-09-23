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
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import requests

import lane
import ucp
from eval.assertions import inband_error
from eval.result_schema import JsonObject, McpResponse, Toolset, as_list, as_object
from functional.checks import (
    CORE_CHECKS,
    DEV_CHECKS,
    LOG_PROBE_TEXT,
    MEDIA_UPLOAD_URL,
    MERCHANT_CHECKS,
    Context,
    ToolCheck,
)
from functional.customer import CustomerUnavailable, provision
from functional.journeys import ORDER_GET, Persona, run_second_order, run_ucp_journey
from functional.principals import PROBE_ARGS, PROBE_TOOL, AdminApi, Principal, ProvisioningFailed, provisioned
from functional.reporting import Reporter
from mcp_client import (
    ALL_TOOLSETS,
    BASE,
    META_TOOLS,
    SW_ACCESS_KEY,
    SW_BASE_URL,
    SW_SC_ACCESS_KEY,
    SW_SECRET_ACCESS_KEY,
    Endpoint,
    admin_endpoint,
    enable_all_toolsets,
    enable_toolset,
    endpoint_by_name,
    mcp_call,
    mcp_call_error,
    mcp_init,
    mcp_list_names,
    mcp_result_text,
    mcp_tools_list_all,
    mcp_toolsets_list,
    store_endpoint,
)

# No extra default-surface tools. A named constant because basedpyright rejects
# a frozenset() call in a parameter default (reportCallInDefaultInitializer).
NO_EXTRA_DEFAULT_TOOLS: frozenset[str] = frozenset()

# typeId of the Storefront sales-channel type (used to find a storefront channel).
STOREFRONT_TYPE_ID = "8a243080f92e4c719546314b577cf82b"


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------
def _payload(resp: McpResponse) -> JsonObject:
    """Parse the JSON payload carried in a tools/call text content block."""
    return lane.payload(resp)


def _advertised(rep: Reporter, session: str, endpoint: Endpoint, label: str) -> list[str] | None:
    """Advertised tool names for a session, or None (after recording a fail)
    when pagination misbehaves."""
    try:
        return [t.get("name", "") for t in mcp_tools_list_all(session, endpoint=endpoint)]
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        rep.check_fail(label, str(exc))
        return None


def _first_field(
    session: str, endpoint: Endpoint, entity: str, field: str = "id", extra: JsonObject | None = None
) -> str:
    """First entity's field value via entity-search, retried a few times
    (a large payload can occasionally deliver a partial read). '' on failure."""
    for _ in range(3):
        args: JsonObject = {"entity": entity, "limit": 1}
        if extra:
            args.update(extra)
        items = lane.data_rows(mcp_call(session, "shopware-entity-search", args, endpoint=endpoint))
        if items:
            value = str(as_object(items[0]).get(field, ""))
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
    args: JsonObject,
    label: str | None = None,
    contains: str = "",
) -> JsonObject:
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
    error = (resp.get("error") or {}).get("message", "")
    content = (resp.get("result") or {}).get("content", [])
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
    rep: Reporter, session: str, endpoint: Endpoint, tool: str, args: JsonObject, expected: str, label: str
) -> None:
    """Call a tool and expect it to FAIL (protocol error, isError, or a
    {"success": false} payload), optionally containing `expected`."""
    resp = mcp_call(session, tool, args, endpoint=endpoint)
    msg = (resp.get("error") or {}).get("message", "")
    result = resp.get("result") or {}
    text = mcp_result_text(resp)
    is_error = bool(msg) or bool(result.get("isError"))
    if not is_error:
        try:
            is_error = as_object(cast(object, json.loads(text))).get("success") is False
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
def verify_default_surface(
    rep: Reporter, session: str, endpoint: Endpoint, also_expected: frozenset[str] = NO_EXTRA_DEFAULT_TOOLS
) -> None:
    """What a fresh session must advertise: the three meta-tools, plus whatever
    the endpoint publishes by design.

    On admin that second set is empty — every catalogue tool is deferred. On the
    Store endpoint it is the thirteen UCP tools, which agentic-commerce 1.3.0
    (UCP 2026-08-25) moved onto the default surface: a UCP client is specified to
    find them by name at connect time, so deferring them behind a toolset would
    have made the endpoint non-conformant.

    The set is passed in rather than read from the endpoint name, so "a deferred
    tool leaked" and "a tool this endpoint publishes" stay distinguishable. That
    distinction is the whole value of the check — without it the leak assertion
    would have to be dropped on the Store endpoint entirely.
    """
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

    missing = also_expected - adv
    if also_expected and not missing:
        rep.check_pass(f"all {len(also_expected)} default-published tools advertised")
    elif missing:
        rep.check_fail("default surface", "published tools not advertised: " + " ".join(sorted(missing)))

    extras = adv - META_TOOLS - also_expected
    if not extras:
        rep.check_pass("no deferred tools leak into the default surface")
    else:
        rep.check_fail("default surface", "unexpected tools advertised: " + " ".join(sorted(extras)))


def load_toolsets(session: str, endpoint: Endpoint) -> list[Toolset]:
    return mcp_toolsets_list(session, endpoint=endpoint)


def verify_connect_time_toolsets(
    rep: Reporter, endpoint: Endpoint, also_expected: frozenset[str] = NO_EXTRA_DEFAULT_TOOLS
) -> None:
    """`?toolsets=` pins toolsets before the first tools/list (shopware#20509).

    This is the only mechanism that can work for a client like claude.ai, which
    reads tools/list once per connection and never again — `toolset-enable`
    always arrives too late for it. So what is under test is not "does enabling
    work" but "is the catalogue already correct on the FIRST enumeration", and
    every case below therefore opens a fresh session and never calls
    toolset-enable at all.

    Toolset names are read off the live server rather than hardcoded: the
    taxonomy is regrouped upstream from time to time, and a hardcoded name would
    turn that into a failure here rather than a finding.
    """
    rep.section("v2: Connect-time toolset selection (?toolsets=)")

    probe, _ = mcp_init(endpoint=endpoint)
    toolsets = load_toolsets(probe, endpoint)
    named = {ts["name"]: set(ts.get("tools", [])) for ts in toolsets if ts.get("tools")}
    if not named:
        rep.skip("connect-time toolsets (this endpoint defers nothing)")
        return

    # Everything a fresh session already sees, so each case asserts only what the
    # parameter ADDED. On store that is the meta-tools plus the UCP tools.
    floor = set(META_TOOLS) | set(also_expected)

    def advertised_with(*names: str) -> set[str] | None:
        pinned = endpoint.with_toolsets(*names)
        try:
            session, _ = mcp_init(endpoint=pinned)
        except (RuntimeError, requests.exceptions.RequestException) as exc:
            rep.check_fail(f"?toolsets={','.join(names)}", f"could not open a session: {exc}")
            return None
        got = _advertised(rep, session, pinned, f"?toolsets={','.join(names)} tools/list")
        return set(got) if got is not None else None

    # One toolset: exactly its tools, and nothing from any sibling.
    first = sorted(named)[0]
    if (got := advertised_with(first)) is not None:
        expected = floor | named[first]
        if got == expected:
            rep.check_pass(f"?toolsets={first} advertises exactly that toolset ({len(named[first])} tools)")
        else:
            rep.check_fail(
                f"?toolsets={first}",
                f"missing: {sorted(expected - got)} unexpected: {sorted(got - expected)}",
            )

    # Several: the union, which is the case a real client actually sends.
    several = sorted(named)[:4]
    if len(several) > 1 and (got := advertised_with(*several)) is not None:
        expected = floor | {tool for n in several for tool in named[n]}
        if got == expected:
            rep.check_pass(f"?toolsets={','.join(several)} advertises the union ({len(expected)} tools)")
        else:
            rep.check_fail(
                f"?toolsets={','.join(several)}",
                f"missing: {sorted(expected - got)} unexpected: {sorted(got - expected)}",
            )

    # `all`, spelled out upstream rather than "*" so it survives clients that
    # escape wildcards.
    if (got := advertised_with(ALL_TOOLSETS)) is not None:
        expected = floor | {tool for tools in named.values() for tool in tools}
        if got == expected:
            rep.check_pass(f"?toolsets={ALL_TOOLSETS} advertises the whole catalogue ({len(expected)} tools)")
        else:
            rep.check_fail(
                f"?toolsets={ALL_TOOLSETS}",
                f"missing: {sorted(expected - got)} unexpected: {sorted(got - expected)}",
            )

    # An unknown name must be ignored, not fatal. A client that pins a toolset
    # the shop does not have — a plugin it lacks — has to keep the rest.
    if (got := advertised_with("no-such-toolset", first)) is not None:
        expected = floor | named[first]
        if got == expected:
            rep.check_pass(f"?toolsets=no-such-toolset,{first} ignores the unknown name")
        else:
            rep.check_fail(
                "?toolsets with an unknown name",
                f"missing: {sorted(expected - got)} unexpected: {sorted(got - expected)}",
            )

    # And a plain connect is unchanged, asserted last so a regression in the
    # parameter cannot be mistaken for one in the default surface.
    if (got := advertised_with()) is not None:
        if got == floor:
            rep.check_pass("a plain connect is unaffected by the feature")
        else:
            rep.check_fail("plain connect", f"default surface moved: {sorted(got ^ floor)}")


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


def _search_payload_tools(session: str, endpoint: Endpoint) -> list[JsonObject]:
    """Tool definitions embedded in shopware-tool-search results.

    Queried across several terms so parameterless tools — the ones that trip the
    empty-properties bug — are actually reached; a single query returns only its
    top matches.
    """
    seen: dict[str, JsonObject] = {}
    for query in ("list", "skills", "search", "config", "order", "product"):
        payload = mcp_result_text(mcp_call(session, "shopware-tool-search", {"query": query}, endpoint=endpoint))
        try:
            data = as_list(as_object(cast(object, json.loads(payload))).get("data"))
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        for row in data:
            tool = as_object(as_object(row).get("tool"))
            name = str(tool.get("name", ""))
            if name:
                seen[name] = tool
    return list(seen.values())


def _malformed_schemas(tools: Sequence[Mapping[str, object]]) -> list[str]:
    """Names the tools whose inputSchema is not a JSON-Schema-valid object.

    Takes a read-only mapping so both callers fit: tools/list hands over typed
    ToolDefs, and tool-search's payload arrives as an untyped JsonObject.
    """
    malformed: list[str] = []
    for tool in tools:
        raw = tool.get("inputSchema")
        # as_object before the isinstance: narrowing an `object` to `dict` yields
        # dict[Unknown, Unknown], and every read off it is then unknown too.
        schema = as_object(raw)
        if not isinstance(raw, dict):
            malformed.append(f"{tool.get('name')}: inputSchema is {type(raw).__name__}")
            continue
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            malformed.append(f"{tool.get('name')}: properties is a {type(properties).__name__}, not an object")
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
    if payload.get("success") and as_object(payload.get("_meta")).get("listChanged"):
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


def run_search(session: str, endpoint: Endpoint, query: str, max_results: int) -> JsonObject:
    return _payload(
        mcp_call(session, "shopware-tool-search", {"query": query, "maxResults": max_results}, endpoint=endpoint)
    )


# ---------------------------------------------------------------------------
# Admin endpoint
# ---------------------------------------------------------------------------
def verify_admin_toolsets(rep: Reporter, session: str, endpoint: Endpoint) -> tuple[str, list[Toolset]]:
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


def verify_admin_discovery(rep: Reporter, endpoint: Endpoint, entity_toolset: str, toolsets: list[Toolset]) -> None:
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
    results = [as_object(r) for r in as_list(search.get("data"))]
    meta = as_object(search.get("_meta"))
    problems: list[str] = []
    if not search.get("success"):
        problems.append("success != true")
    if not results:
        problems.append("no results")
    if any(not all(k in r for k in ("tool", "score", "matchedIn")) for r in results):
        problems.append("result missing tool/score/matchedIn")
    names = [str(as_object(r.get("tool")).get("name", "")) for r in results]
    if "shopware-media-upload" not in names:
        problems.append(f"shopware-media-upload not in results: {names}")
    if "query" not in meta or "totalCandidates" not in meta:
        problems.append("_meta missing query/totalCandidates")
    if not problems:
        rep.check_pass("tool-search finds deferred shopware-media-upload with score/matchedIn")
    else:
        rep.check_fail("tool-search (upload an image file)", "; ".join(problems))

    # tool-search caps maxResults at 20
    cap_count = len(as_list(run_search(session, endpoint, "shopware", 50).get("data")))
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


def run_checks(rep: Reporter, session: str, endpoint: Endpoint, checks: tuple[ToolCheck, ...], ctx: Context) -> None:
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


def _served(url: str) -> str:
    """`url` if something is actually served there, else "".

    HEAD first, GET as the fallback — a server that answers 405 to HEAD is
    common enough that treating it as absent would skip the check on a lane
    that was seeded correctly.

    Probed from HERE, while the tool fetches it from the SHOP. Those are the same
    machine in CI, and on any lane this suite can talk to they agree about the
    shop's own URL. Where they do not, the FAIL is the informative outcome and
    eval/preflight.py already names that class of problem — a server that cannot
    reach its own published address.
    """
    for request in (requests.head, requests.get):
        try:
            if request(url, timeout=5, allow_redirects=True).status_code < 400:
                return url
        except requests.RequestException:
            return ""
    return ""


def gather_context(session: str, endpoint: Endpoint, args: argparse.Namespace) -> Context:
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
        "media_upload_enabled": not cast(bool, args.skip_media_upload),
        # Empty when nothing is served there, which the check declares a
        # prerequisite: an image the lane never seeded is missing setup, not
        # evidence that shopware-media-upload is broken.
        "media_upload_url": _served(MEDIA_UPLOAD_URL),
    }


def sellable_products(session: str, endpoint: Endpoint, sales_channel_id: str) -> list[str]:
    """Storefront-sellable product candidates. See lane.sellable_products.

    Wrapped rather than imported bare so the eval and functional suites cannot
    drift apart on what "sellable" means again — eval/ was still adding the
    first search hit and trusting `success: true` long after this suite learned
    that answers 200 with an empty cart.
    """
    return lane.sellable_products(session, endpoint, sales_channel_id)


LOG_FILE_ENV = "MCP_EVALS_LOG_FILE"


def newest_log_file(session: str, endpoint: Endpoint) -> tuple[str, str]:
    """A log file the dev-tools log readers can open, and why there is none.

    Returns `(file, reason)` — exactly one is ever non-empty. The reason exists
    because the old signature could only say "" and the caller then reported *no
    log files on this instance*, a claim about the shop that this function never
    checked. It was wrong in both directions we have actually seen: a CI lane that
    had seeded a log file, and a local lane where `SwagMcpDevTools` was not
    installed so the tool did not exist at all.

    `MCP_EVALS_LOG_FILE` wins when set. The lane seeds a known line into
    `var/log/<env>-<date>.log` during setup and now exports that name, so on our
    own lanes the filename is *known* rather than recovered — the discovery below
    reverse-engineers it out of an error message, and an error message is the
    least stable part of any tool's contract.

    The discovery stays for shops we did not build: `file` defaults to an empty
    string, which is never a real filename, and the tool helpfully lists the valid
    values in the resulting error.
    """
    if named := os.environ.get(LOG_FILE_ENV, "").strip():
        return named, ""

    reply = mcp_call(session, "swag-dev-tools-log-search", {"query": "x", "limit": 1}, endpoint=endpoint)
    if error := as_object(reply.get("error")).get("message"):
        # A missing tool is not a missing log. This is what a lane without
        # SwagMcpDevTools answers, and reporting it as an absent log file sends
        # the reader to the seeding step, which is fine.
        return "", f"swag-dev-tools-log-search is not callable: {error}"

    text = mcp_result_text(reply)
    marker = "Available files:"
    if marker not in (text or ""):
        # The tool answered, and not with the list this parse depends on. Naming
        # that is the whole point: the contract moved, and no amount of seeding
        # will fix it.
        return "", f"the tool answered without an {marker!r} list, so no filename could be read"

    listed = text.split(marker, 1)[1].strip().rstrip('"}').split(",")
    files = [f.strip().strip('"') for f in listed if f.strip()]
    if not files:
        return "", "the tool lists no log files on this instance"

    # max(), not files[-1]. A dated name sorts chronologically so the newest wins
    # either way IF the server returns them ordered — and nothing promises that.
    # Taking the last element made a correct-looking result depend on an
    # undocumented detail of somebody else's response.
    return max(files), ""


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
    raw = _payload(mcp_call(session, "swag-dev-tools-list-skills", {}, endpoint=endpoint)).get("data")
    data = as_object(raw)
    skills = as_list(data.get("skills", raw)) if data else as_list(raw)
    if not skills:
        return ""
    first = skills[0]
    # as_object before the isinstance, for the same reason as _malformed_schemas.
    named = as_object(first)
    return str(named.get("name", "")) if isinstance(first, dict) else str(first)


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
    channel = str(ctx.get("sales_channel_id", ""))
    product_ids = sellable_products(session, endpoint, channel)
    ctx["cart_product_ids"] = product_ids
    ctx["cart_token"] = create_cart_token(session, endpoint, channel, product_ids)
    run_checks(rep, session, endpoint, MERCHANT_CHECKS, ctx)

    rep.section("Dev tools")
    if cast(bool, args.skip_dev_tools):
        rep.skip("dev tools (--skip-dev-tools)")
        return
    log_file, ctx["log_file_reason"] = newest_log_file(session, endpoint)
    ctx["log_file"], ctx["log_probe"] = find_log_probe(session, endpoint, log_file) if log_file else ("", False)
    # Needs the tool it is named after to have run, so it cannot be part of ctx.
    ctx["skill_name"] = first_skill_name(session, endpoint)
    run_checks(rep, session, endpoint, DEV_CHECKS, ctx)


# ---------------------------------------------------------------------------
# Allowlist matrix: what each kind of principal can reach
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Catalogue:
    """What the suite's own principal reaches, which every other one is measured against.

    That principal is an administrator user, so the allowlist does not apply to
    it (#20600): this is the whole catalogue, and "All capabilities" on an
    integration has to reach exactly this much.
    """

    toolsets: Mapping[str, frozenset[str]]
    tools: set[str]
    resources: set[str]
    prompts: set[str]
    searched: set[str]

    def toolset_of(self, tool: str) -> str:
        return next((name for name, tools in sorted(self.toolsets.items()) if tool in tools), "")

    def control_tool(self) -> str:
        """A tool from a toolset other than the probe's: what a one-tool grant must not reach."""
        probe = self.toolset_of(PROBE_TOOL)
        return next((min(tools) for name, tools in sorted(self.toolsets.items()) if name != probe and tools), "")


def load_catalogue(endpoint: Endpoint) -> Catalogue:
    session, _ = mcp_init(endpoint=endpoint)
    toolsets = {ts["name"]: frozenset(ts.get("tools", [])) for ts in mcp_toolsets_list(session, endpoint=endpoint)}
    pinned = endpoint.with_toolsets(ALL_TOOLSETS)
    pinned_session, _ = mcp_init(endpoint=pinned)
    return Catalogue(
        toolsets=toolsets,
        tools={t.get("name", "") for t in mcp_tools_list_all(pinned_session, endpoint=pinned)},
        resources=set(mcp_list_names(session, "resources/list", endpoint=endpoint)),
        prompts=set(mcp_list_names(session, "prompts/list", endpoint=endpoint)),
        searched=_searched(session, endpoint),
    )


# Broad on purpose: the check is about which names tool-search may surface for a
# principal, not about ranking, so it wants as much of the catalogue as it will
# return. verify_admin_discovery uses the same query for its result cap.
SEARCH_QUERY = "shopware"


def _searched(session: str, endpoint: Endpoint) -> set[str]:
    result = run_search(session, endpoint, SEARCH_QUERY, 50)
    names = {str(as_object(as_object(r).get("tool")).get("name", "")) for r in as_list(result.get("data"))}
    return names - META_TOOLS


def _call_error(resp: McpResponse) -> str:
    """Why a call did not run, including the in-band `{"success": false}` kind."""
    return mcp_call_error(resp) or inband_error(mcp_result_text(resp)) or ""


def _refused_by_allowlist(resp: McpResponse) -> bool:
    # Named, not just "errored": an argument-validation error is also an error,
    # and counting it as a refusal would pass a server that enforces nothing.
    return "allowlist" in ((resp.get("error") or {}).get("message", "") + mcp_result_text(resp)).lower()


def _names(items: set[str]) -> str:
    return " ".join(sorted(items)[:8]) + (" …" if len(items) > 8 else "")


def _diff(want: set[str], got: set[str]) -> str:
    return f"missing {_names(want - got) or '-'}, extra {_names(got - want) or '-'}"


def verify_principal(rep: Reporter, principal: Principal, cat: Catalogue, suite: Endpoint) -> None:
    """Every discovery surface, and both call paths, for one principal.

    Each surface is filtered separately on the server (`McpAllowlistListRequestHandler`,
    `McpToolsetRegistry`, `AbstractToolSearchTool`, `McpServerController`), so a
    fix to one leaves the others free to disagree. The report that started this
    was one of them — toolsets-list empty for an integration whose allowlist the
    Administration showed as "All capabilities".
    """
    who = principal.label.split("-", 3)[-1]
    expect = principal.expect

    def check(ok: bool, claim: str, detail: str) -> None:
        if ok:
            rep.check_pass(f"{who}: {claim}")
        else:
            rep.check_fail(f"allowlist: {who}", f"expected it {claim}, but {detail}")

    endpoint = admin_endpoint(principal.access_key, principal.secret_key, base_url=suite.base_url)
    try:
        session, _ = mcp_init(endpoint=endpoint)
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        # Not a pass even for a principal that should be blocked: #20600 lets it
        # open a session and see the meta-tools, so a handshake failure is a bad
        # credential or a broken server — and would leave the unset state
        # untested while reading as green.
        check(False, "opens a session", f"initialize failed: {str(exc)[:80]}")
        return

    probe_toolset = cat.toolset_of(PROBE_TOOL)
    control = cat.control_tool()
    try:
        advertised = set(_advertised(rep, session, endpoint, f"allowlist: {who} tools/list") or ())
        check(advertised == META_TOOLS, "is advertised only the meta-tools by default", f"saw {_names(advertised)}")

        listed = {ts["name"] for ts in mcp_toolsets_list(session, endpoint=endpoint)}
        if expect == "blocked":
            check(not listed, "is listed no toolsets", f"toolsets-list returned {_names(listed)}")
        elif expect == "all":
            check(
                listed == set(cat.toolsets),
                "is listed every toolset",
                _diff(set(cat.toolsets), listed),
            )
        else:
            check(
                probe_toolset in listed and cat.toolset_of(control) not in listed,
                f"is listed {probe_toolset} and not {cat.toolset_of(control)}",
                f"toolsets-list returned {_names(listed) or 'nothing'}",
            )

        enable_error = _call_error(enable_toolset(session, probe_toolset, endpoint=endpoint))
        if expect == "blocked":
            check(bool(enable_error), f"is refused toolset-enable {probe_toolset}", "the toolset was enabled")
        else:
            check(not enable_error, f"can enable {probe_toolset}", enable_error[:120])

        pinned = endpoint.with_toolsets(ALL_TOOLSETS)
        pinned_session, _ = mcp_init(endpoint=pinned)
        reach = {t.get("name", "") for t in mcp_tools_list_all(pinned_session, endpoint=pinned)}
        if expect == "blocked":
            check(reach == META_TOOLS, "reaches only the meta-tools with ?toolsets=all", f"reached {_names(reach)}")
        elif expect == "all":
            check(
                reach == cat.tools,
                f"reaches the whole catalogue ({len(cat.tools)} tools) with ?toolsets=all",
                _diff(cat.tools, reach),
            )
        else:
            check(
                PROBE_TOOL in reach and control not in reach,
                f"reaches {PROBE_TOOL} and not {control} with ?toolsets=all",
                f"reached {_names(reach)}",
            )

        for method, everything in (("resources/list", cat.resources), ("prompts/list", cat.prompts)):
            got = set(mcp_list_names(session, method, endpoint=endpoint))
            want = everything if expect == "all" else set[str]()
            check(got == want, f"gets {len(want)} from {method}", f"got {len(got)}: {_names(got ^ want)}")

        # AbstractToolSearchTool filters on its own; it may only surface what the
        # principal can reach, and for "All" exactly what the administrator's
        # search does.
        searched = _searched(session, endpoint)
        if expect == "all":
            check(searched == cat.searched, "is shown every tool-search hit", _diff(cat.searched, searched))
        else:
            leaked = searched - (reach - META_TOOLS)
            check(not leaked, "is shown only reachable tools by tool-search", f"it surfaced {_names(leaked)}")
        if expect == "partial" and PROBE_TOOL in cat.searched:
            check(PROBE_TOOL in searched, f"is shown {PROBE_TOOL} by tool-search", f"it surfaced {_names(searched)}")

        # Advertising is not the call boundary; a client that knows a name can
        # call it without ever listing anything.
        probe = mcp_call(session, PROBE_TOOL, PROBE_ARGS, endpoint=endpoint)
        if expect == "blocked":
            check(
                _refused_by_allowlist(probe),
                f"is refused {PROBE_TOOL}",
                f"the call answered: {_call_error(probe)[:120] or 'ok'}",
            )
        else:
            check(not _call_error(probe), f"can call {PROBE_TOOL}", _call_error(probe)[:120])
        if expect == "partial" and control:
            refused = _refused_by_allowlist(mcp_call(session, control, {}, endpoint=endpoint))
            check(refused, f"is refused {control}", "the call was not refused by the allowlist")
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        rep.check_fail(f"allowlist: {who}", f"aborted: {exc}")


def verify_allowlist_matrix(rep: Reporter, endpoint: Endpoint, provision: bool) -> None:
    """Integrations and non-admin users, with no allowlist, "All", and one tool.

    shopware/shopware#20600 made the allowlist the gate for every principal
    except an administrator user, and moved this suite onto one. So nothing the
    rest of the run sees depends on the allowlist at all — it could grant
    nothing to anyone, or everything, and every other check would stay green.
    The principals are created per run (functional/principals.py) because what
    "All capabilities" saves is a snapshot of the catalogue, and a list minted
    in setup-lane would drift from the one the Administration writes.
    """
    rep.section("Allowlist matrix (integrations and non-admin users)")
    if not provision:
        rep.skip("allowlist matrix needs --provision-principals: it creates integrations, users and a role")
        return

    try:
        cat = load_catalogue(endpoint)
    except (RuntimeError, requests.exceptions.RequestException) as exc:
        rep.check_fail("allowlist matrix", f"could not read the reference catalogue: {exc}")
        return
    # Everything below compares against this, and an empty reference makes
    # "reaches exactly as much as the administrator" pass for a principal that
    # reaches nothing.
    if cat.tools <= META_TOOLS or not cat.toolset_of(PROBE_TOOL):
        rep.check_fail(
            "allowlist matrix", f"the suite's own principal reaches no {PROBE_TOOL}, so nothing can be compared"
        )
        return

    try:
        api = AdminApi(
            endpoint.base_url, endpoint.auth_headers["sw-access-key"], endpoint.auth_headers["sw-secret-access-key"]
        )
        with provisioned(api) as principals:
            for principal in principals:
                verify_principal(rep, principal, cat, endpoint)
    except ProvisioningFailed as exc:
        rep.check_fail("allowlist matrix", f"could not provision the principals: {exc}")


def run_admin(rep: Reporter, endpoint: Endpoint, args: argparse.Namespace, session: str) -> None:
    verify_allowlist_matrix(rep, endpoint, provision=cast(bool, args.provision_principals))
    verify_default_surface(rep, session, endpoint)
    verify_connect_time_toolsets(rep, endpoint)
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
    # The thirteen UCP tools are published on the default surface by design since
    # agentic-commerce 1.3.0, so they are expected here rather than counted as a
    # leak. ucp.py owns the list.
    verify_default_surface(rep, session, endpoint, also_expected=ucp.all_classified())
    verify_connect_time_toolsets(rep, endpoint, also_expected=ucp.all_classified())

    # --- toolset taxonomy ---
    rep.section("v2: Toolset taxonomy")
    toolsets = load_toolsets(session, endpoint)
    union: set[str] = set()
    deferred_toolset = ""
    deferred_probe = ""
    for ts in toolsets:
        tools = sorted(ts.get("tools", []))
        union.update(tools)
        if tools and not deferred_toolset:
            deferred_toolset = ts["name"]
            deferred_probe = tools[0]

    # There used to be several granular UCP toolsets here (cart, checkout,
    # catalog, ...) and this asserted >= 2. agentic-commerce 1.3.0 published the
    # UCP tools on the default surface instead and the UCP toolsets went with
    # them, so `store-api` holding shopware-store-api-context is the only one
    # left. The floor is 1 rather than a hardcoded name so a resliced taxonomy
    # still reports rather than crashing.
    if toolsets:
        rep.check_pass(f"toolsets-list returns {len(toolsets)} toolset(s): {', '.join(ts['name'] for ts in toolsets)}")
    else:
        rep.check_fail("toolsets-list", "no toolsets at all")

    # The UCP tools are checked on the default surface (above), not here. What is
    # left to prove about the taxonomy is that something is still deferred behind
    # it — if this reaches zero, enable/isolation below has nothing to exercise
    # and the endpoint's discovery layer is untested rather than passing.
    if union:
        rep.check_pass(f"toolsets defer {len(union)} tool(s) off the default surface")
    else:
        rep.check_fail("toolset coverage", "no toolset defers anything; nothing left to enable")

    # --- enable grows the list + listChanged; session isolation ---
    rep.section("v2: Discovery mechanics")
    if deferred_toolset:
        verify_enable_and_isolation(
            rep,
            endpoint,
            deferred_toolset,
            probe_tool=deferred_probe,
            probe_label=deferred_probe,
            check_default_persists=False,
        )
    else:
        rep.skip("enable/isolation (no toolset defers anything)")

    # --- store-api-context: deferred but directly callable ---
    rep.section("Store context & search")
    ctx_session, _ = mcp_init(endpoint=endpoint)
    ctx = _payload(mcp_call(ctx_session or session, "shopware-store-api-context", {}, endpoint=endpoint))
    data = as_object(ctx.get("data"))
    if ctx.get("success") and data.get("salesChannelId") and data.get("token"):
        rep.check_pass("shopware-store-api-context (deferred, callable, returns channel+token)")
    else:
        rep.check_fail("shopware-store-api-context", "missing salesChannelId/token or errored")

    # --- tool-search ranks the right UCP tool ---
    #
    # No longer "finds a DEFERRED tool" — these are advertised by default now, so
    # search is not how a client reaches them. It is still worth one check: the
    # ranking is what a client falls back on when thirteen adjacent tools are all
    # visible at once, which is the harder problem, not the easier one.
    cart_tools = {"create_cart", "get_cart", "update_cart", "cancel_cart"}
    search = run_search(session, endpoint, "add items to a shopping cart", 5)
    names = [str(as_object(as_object(r).get("tool")).get("name", "")) for r in as_list(search.get("data"))]
    if search.get("success") and cart_tools.intersection(names):
        rep.check_pass("shopware-tool-search ranks a UCP cart tool for a cart query")
    else:
        rep.check_fail("shopware-tool-search", f"no UCP cart tool in results: {names}")

    # --- the buyer journey ---
    #
    # This is where the UCP tools are actually exercised. Everything above tests
    # the discovery layer around them; only the journey tests the tools, because
    # they are one flow and an isolated call to any of them mostly proves how the
    # server words "not found".
    rep.section("UCP buyer journey — guest")
    journey_session, _ = mcp_init(endpoint=endpoint)
    enable_all_toolsets(journey_session, endpoint=endpoint)
    run_ucp_journey(rep, journey_session, endpoint, allow_mutations=allow_mutations)

    # --- the same journey, as a real customer ---
    #
    # The guest flow cannot read its own order back — correctly, and the journey
    # asserts that refusal. So without this half, nothing in the suite ever proves
    # order-get returns an order at all, and its fixtures would be graded on a
    # tool that has only ever said no.
    rep.section("UCP buyer journey — authenticated customer")
    run_customer_journey(rep, allow_mutations)


def run_customer_journey(rep: Reporter, allow_mutations: bool) -> None:
    """The journey again, logged in as a real customer.

    A failure to provision is reported against `order-get` specifically, not
    against every tool the journey touches: the guest run already proved those
    work, and the read-back is the only coverage this half is uniquely
    responsible for. `skipped` outranks `pass` in the health map, so order-get
    then reads as "nobody proved it works", which is exactly true.
    """
    if not allow_mutations:
        rep.skip("customer journey needs --allow-mutations: it registers a customer and places a real order")
        return

    try:
        email, context_token = provision(SW_BASE_URL, SW_SC_ACCESS_KEY)
    except CustomerUnavailable as exc:
        rep.skip(f"customer journey: {exc}")
        rep.tool_skip(ORDER_GET, f"{ORDER_GET} (customer: read the placed order back)", f"read-back unproven: {exc}")
        return

    rep.info(f"  shopping as {email}")
    # A second endpoint rather than a mutated one: the guest journey's cart lives
    # on the process-wide token, and rebuilding that would silently abandon it.
    customer_endpoint = store_endpoint(context_token=context_token)
    session, _ = mcp_init(endpoint=customer_endpoint)
    enable_all_toolsets(session, endpoint=customer_endpoint)

    ctx = run_ucp_journey(
        rep,
        session,
        customer_endpoint,
        allow_mutations=allow_mutations,
        persona=Persona("customer", context_token),
    )

    # Ordering once is the easy half. A buyer who comes back is the half that has
    # been broken, so the suite asks for it rather than assuming.
    run_second_order(rep, session, customer_endpoint, ctx)


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
    parser.add_argument(
        "--provision-principals",
        action="store_true",
        help=(
            "Create throwaway integrations, non-admin users and an ACL role to check what each "
            "allowlist state reaches; they are deleted afterwards. Admin endpoint only."
        ),
    )
    args = parser.parse_args()
    endpoint_name = cast(str, args.endpoint)

    require("SW_BASE_URL", SW_BASE_URL)
    endpoint = endpoint_by_name(endpoint_name)
    if endpoint_name == "admin":
        require("SW_ACCESS_KEY", SW_ACCESS_KEY)
        require("SW_SECRET_ACCESS_KEY", SW_SECRET_ACCESS_KEY)
    else:
        require("SW_SC_ACCESS_KEY (sales-channel access key)", SW_SC_ACCESS_KEY)

    rep = Reporter(SW_BASE_URL)
    rep.banner(f"Shopware MCP Functional Tests — {endpoint_name} (v2 discovery)")
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
        if endpoint_name == "admin":
            run_admin(rep, endpoint, args, session)
        else:
            run_store(rep, endpoint, session, allow_mutations=cast(bool, args.allow_mutations))
    except requests.exceptions.RequestException as exc:
        # A transport failure that survived the client's throttle retries — record
        # it and still emit a summary + report rather than crashing mid-suite.
        rep.check_fail("transport", f"request failed, suite aborted early: {exc}")

    rep.summary()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rep.write_report(BASE / "results" / f"functional-{endpoint_name}-{timestamp}.json")
    # Stable filename: the eval job consumes this as an artifact, so it cannot
    # be timestamped like the report.
    rep.write_health(BASE / "results" / f"tool-health-{endpoint_name}.json")
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
