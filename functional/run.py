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
  python functional/run.py [--endpoint admin] [--skip-media-upload] [--skip-dev-tools]
  python functional/run.py --endpoint store

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import requests

# mcp_client lives at the repo root (shared by the eval and functional layers);
# reporting lives alongside this script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_client import (  # noqa: E402
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
from reporting import Reporter  # noqa: E402

# A phantom UUID that cannot exist — used for dryRun delete assertions.
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
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
    except ValueError, TypeError:
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
    rep: Reporter, session: str, endpoint: Endpoint, tool: str, args: dict, label: str | None = None
) -> None:
    """Call a tool; pass if there is no protocol error and content is present."""
    label = label or tool
    resp = mcp_call(session, tool, args, endpoint=endpoint)
    error = resp.get("error", {}).get("message", "")
    content = resp.get("result", {}).get("content", [])
    if error:
        rep.tool_fail(tool, label, error)
    elif not content:
        rep.tool_fail(tool, label, "empty content in response")
    else:
        rep.tool_pass(tool, label, mcp_result_text(resp)[:120])


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
        except ValueError, TypeError:
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
        except json.JSONDecodeError, TypeError, AttributeError:
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


def run_admin_tools(rep: Reporter, session: str, endpoint: Endpoint, args: argparse.Namespace) -> None:
    """Per-tool assertions on a session with NO toolsets enabled — each call to
    a deferred tool doubles as a direct-callability assertion."""
    product_id = _first_field(session, endpoint, "product")
    order_id = _first_field(session, endpoint, "order")
    customer_email = _first_field(session, endpoint, "customer", field="email")
    sales_channel_id = _first_field(
        session,
        endpoint,
        "sales_channel",
        extra={
            "criteria": json.dumps({"filter": [{"type": "equals", "field": "typeId", "value": STOREFRONT_TYPE_ID}]})
        },
    )

    # --- Core tools ---
    rep.section("Core tools")
    assert_tool(
        rep, session, endpoint, "shopware-entity-schema", {"entity": "product"}, "shopware-entity-schema (product)"
    )
    assert_tool(
        rep,
        session,
        endpoint,
        "shopware-entity-search",
        {"entity": "product", "limit": 1},
        "shopware-entity-search (product, limit 1)",
    )
    if product_id:
        assert_tool(
            rep,
            session,
            endpoint,
            "shopware-entity-read",
            {"entity": "product", "id": product_id},
            "shopware-entity-read (product by ID)",
        )
    else:
        rep.skip("shopware-entity-read (no product found)")
    assert_tool(
        rep,
        session,
        endpoint,
        "shopware-entity-aggregate",
        {"entity": "product", "aggregations": json.dumps([{"name": "total", "type": "count", "field": "id"}])},
        "shopware-entity-aggregate (count products)",
    )
    if product_id:
        assert_tool(
            rep,
            session,
            endpoint,
            "shopware-entity-upsert",
            {"entity": "product", "payload": json.dumps({"id": product_id, "stock": 1}), "dryRun": True},
            "shopware-entity-upsert (dryRun)",
        )
    else:
        rep.skip("shopware-entity-upsert (no product found)")
    assert_tool(
        rep,
        session,
        endpoint,
        "shopware-entity-delete",
        {"entity": "product", "ids": json.dumps([ZERO_UUID]), "dryRun": True},
        "shopware-entity-delete (dryRun)",
    )
    assert_tool(
        rep,
        session,
        endpoint,
        "shopware-system-config-read",
        {"key": "core.basicInformation"},
        "shopware-system-config-read",
    )
    assert_tool(
        rep,
        session,
        endpoint,
        "shopware-system-config-write",
        {"key": "core.basicInformation.shopName", "value": json.dumps("Test"), "dryRun": True},
        "shopware-system-config-write (dryRun)",
    )
    if order_id:
        assert_tool(
            rep,
            session,
            endpoint,
            "shopware-order-state",
            {"orderId": order_id, "dryRun": True},
            "shopware-order-state (dryRun)",
        )
    else:
        rep.skip("shopware-order-state (no order found)")
    if args.skip_media_upload:
        rep.skip("shopware-media-upload (--skip-media-upload)")
    else:
        assert_tool(
            rep,
            session,
            endpoint,
            "shopware-media-upload",
            {"url": "https://assets.shopware.com/media/shopware_signet_blue.svg", "fileName": "mcp-test-logo"},
            "shopware-media-upload",
        )
    if sales_channel_id:
        assert_tool(
            rep,
            session,
            endpoint,
            "shopware-theme-config",
            {"salesChannelId": sales_channel_id, "action": "get"},
            "shopware-theme-config (get)",
        )
    else:
        rep.skip("shopware-theme-config (no storefront sales channel found)")

    # --- Merchant tools ---
    rep.section("Merchant tools")
    if customer_email:
        assert_tool(
            rep,
            session,
            endpoint,
            "merchant-customer-lookup",
            {"email": customer_email},
            "merchant-customer-lookup (by email)",
        )
    else:
        rep.skip("merchant-customer-lookup (no customer found)")
    if order_id:
        assert_tool(
            rep, session, endpoint, "merchant-order-summary", {"orderId": order_id}, "merchant-order-summary (by ID)"
        )
    else:
        rep.skip("merchant-order-summary (no order found)")
    if sales_channel_id:
        assert_tool(
            rep,
            session,
            endpoint,
            "merchant-checkout-methods",
            {"salesChannelId": sales_channel_id},
            "merchant-checkout-methods",
        )
        assert_tool(
            rep,
            session,
            endpoint,
            "merchant-storefront-search",
            {"salesChannelId": sales_channel_id, "term": "shirt"},
            "merchant-storefront-search (term: shirt)",
        )
        cart_token = (
            _payload(
                mcp_call(
                    session,
                    "merchant-cart-manage",
                    {"salesChannelId": sales_channel_id, "action": "create"},
                    endpoint=endpoint,
                )
            )
            .get("data", {})
            .get("token", "")
        )
        assert_tool(
            rep,
            session,
            endpoint,
            "merchant-cart-manage",
            {"salesChannelId": sales_channel_id, "action": "create"},
            "merchant-cart-manage (create)",
        )
        customer_id = _first_field(session, endpoint, "customer")
        if cart_token and customer_id:
            assert_tool(
                rep,
                session,
                endpoint,
                "merchant-cart-checkout",
                {"salesChannelId": sales_channel_id, "token": cart_token, "customerId": customer_id, "dryRun": True},
                "merchant-cart-checkout (dryRun)",
            )
        else:
            rep.skip("merchant-cart-checkout (could not get cart token or customer ID)")
    else:
        rep.skip("merchant-checkout-methods (no storefront sales channel)")
        rep.skip("merchant-storefront-search (no storefront sales channel)")
        rep.skip("merchant-cart-manage (no storefront sales channel)")
        rep.skip("merchant-cart-checkout (no storefront sales channel)")
    assert_tool(
        rep,
        session,
        endpoint,
        "merchant-product-create",
        {"name": "MCP Test Product", "productNumber": "MCP-TEST-001", "grossPrice": 9.99, "dryRun": True},
        "merchant-product-create (dryRun)",
    )
    assert_tool(
        rep,
        session,
        endpoint,
        "merchant-bestseller-report",
        {"from": "2025-01-01", "to": "2025-12-31", "limit": 5},
        "merchant-bestseller-report",
    )
    assert_tool(
        rep,
        session,
        endpoint,
        "merchant-revenue-report",
        {"from": "2025-01-01", "to": "2025-12-31", "groupBy": "month"},
        "merchant-revenue-report (groupBy month)",
    )

    # --- Dev tools ---
    rep.section("Dev tools")
    if args.skip_dev_tools:
        rep.skip("dev tools (--skip-dev-tools)")
        return
    assert_tool(
        rep,
        session,
        endpoint,
        "swag-dev-tools-log-search",
        {"query": "error", "limit": 5},
        "swag-dev-tools-log-search (query: error)",
    )
    assert_tool(
        rep, session, endpoint, "swag-dev-tools-log-stream", {"limit": 10}, "swag-dev-tools-log-stream (last 10)"
    )
    assert_tool(rep, session, endpoint, "swag-dev-tools-list-extensions", {}, "swag-dev-tools-list-extensions")
    assert_tool(rep, session, endpoint, "swag-dev-tools-list-skills", {}, "swag-dev-tools-list-skills")
    # scaffold with no args lists the available types — non-destructive.
    assert_tool(rep, session, endpoint, "swag-dev-tools-scaffold", {}, "swag-dev-tools-scaffold (list types)")
    # notifications: wait=false so it never opens an SSE stream in tests.
    assert_tool(
        rep,
        session,
        endpoint,
        "swag-dev-tools-notifications",
        {"wait": False, "limit": 5},
        "swag-dev-tools-notifications (poll)",
    )
    # load-skill needs a real skill name — pull the first one from list-skills.
    data = _payload(mcp_call(session, "swag-dev-tools-list-skills", {}, endpoint=endpoint)).get("data", {})
    skills = data.get("skills", data) if isinstance(data, dict) else data
    skill_name = ""
    if isinstance(skills, list) and skills:
        first = skills[0]
        skill_name = first.get("name", "") if isinstance(first, dict) else str(first)
    if skill_name:
        assert_tool(
            rep,
            session,
            endpoint,
            "swag-dev-tools-load-skill",
            {"name": skill_name},
            f"swag-dev-tools-load-skill ({skill_name})",
        )
    else:
        rep.skip("swag-dev-tools-load-skill (no skills found)")


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
def run_store(rep: Reporter, endpoint: Endpoint, session: str) -> None:
    """The Store API endpoint uses the same v2 discovery mechanics, but does not
    execute the UCP cart/checkout/catalog tools (those need provisioned state;
    tool selection for them is covered by the LLM eval)."""
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
            run_store(rep, endpoint, session)
    except requests.exceptions.RequestException as exc:
        # A transport failure that survived the client's throttle retries — record
        # it and still emit a summary + report rather than crashing mid-suite.
        rep.check_fail("transport", f"request failed, suite aborted early: {exc}")

    rep.summary()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rep.write_report(BASE / "results" / f"functional-{args.endpoint}-{timestamp}.json")
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
