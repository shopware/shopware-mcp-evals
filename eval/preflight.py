#!/usr/bin/env python3
"""Prove one tool actually executes before spending an LLM budget on the suite.

This exists because of a specific failure that went unnoticed for the entire
life of the Store suite: every UCP runtime tool rejected every call, and the
suite still reported a pass rate. Selection-only grading cannot tell "the model
picked the right tool" from "the model picked the right tool and the server
refused it", so 42 fixtures graded green while nothing underneath them ran.

Execution (Phase 3) turned those into failures rather than silence, but a failure
that shows up as 42 red fixtures is still the wrong shape: it costs a full LLM
pass to learn something one call would have told us, and it reads as a
tool-quality result when it is an environment problem.

So: call one read-only tool directly, no model involved, and fail loudly with a
diagnosis. Runs in about a second and costs nothing.
"""

import argparse
import json
import sys
import time

import requests

import mcp_client as mc
import toolclass

# A read-only tool per endpoint whose success proves the whole chain works:
# session, toolset enable, argument validation, and — on store — the UCP-Agent
# header, its profile fetch, and both allowlists.
#
# The store query is a placeholder: run() replaces it with a product name
# discovered from the Store API (see discover_store_query), so the probe searches
# something the shop actually has rather than an invented word.
PROBES = {
    "store": ("shopware-ucp-catalog-search", {"query": "test"}),
    "admin": ("shopware-entity-search", {"entity": "product", "limit": 1}),
}

# The store path does not stop at search. Once search returns a product it takes
# that product's id and looks it up, because lookup fails differently: search
# tolerates a query that matches nothing (empty is a valid answer), while lookup
# needs a real id and is exactly what the pinned agentic-commerce branch was
# fixing. The id comes from search's own result, not the Store API, because UCP
# may namespace product ids differently — this mirrors functional/journeys.py,
# where `ids` is a single string, not an array.
STORE_LOOKUP_TOOL = "shopware-ucp-catalog-lookup"

# The Store API product-listing route, called with the sales-channel access key
# the store endpoint already uses.
STORE_PRODUCT_ROUTE = "/store-api/product"

# Error text -> what is actually wrong and where to fix it. Every one of these
# cost at least one debugging round to identify the first time.
DIAGNOSES = (
    (
        "ucp-agent header",
        "The client sent no UCP-Agent header. See ucp.agent_header — mcp_client "
        "should be building one for every store endpoint.",
    ),
    (
        "plain http is only allowed",
        "The profile URI is http and its host is not exactly localhost/127.0.0.1/::1. "
        "Either point APP_URL at localhost, or set "
        "SWAG_AGENTIC_COMMERCE_UCP_PROFILE_FETCHING_DEVELOPMENT_MODE=1 and use a host "
        "the SDK's isLocalHost() accepts — it does not accept `.localhost` subdomains.",
    ),
    (
        "platform profile host",
        "The profile URI's host is not in platformAllowlist, which falls back to the "
        "host of the incoming request. Either serve the profile on the same host the "
        "suite calls, or set it explicitly: "
        "bin/console ucp:config:set --sales-channel=<id> --platform-allowlist=<host>",
    ),
    (
        "agent",
        "The profile host is not in agentAllowlist. Set it with: "
        "bin/console ucp:config:set --sales-channel=<id> --agent-allowlist=<host>",
    ),
    (
        "missing signature headers",
        "signaturePolicy is 'strict' — its default — so every UCP call must carry RFC 9421 "
        "HTTP message signatures, which this suite does not send. On a throwaway instance: "
        "bin/console ucp:config:set --sales-channel=<id> --signature-policy=off",
    ),
    (
        "idempotency key is required",
        "A mutating call went out without an Idempotency-Key. See ucp.call_headers.",
    ),
    (
        "internal",
        "The plugin swallowed the real exception (UcpMcpToolContext::failure reports "
        "`internal` and logs nothing). Common cause: the server cannot fetch the "
        "profile URI from inside its own container — a published host:port is not "
        "necessarily reachable there. Check with a request from inside the container.",
    ),
)


def diagnose(error: str) -> str:
    """Map a failure to its cause. Pure, so the table is testable without a server."""
    lowered = (error or "").lower()
    for marker, advice in DIAGNOSES:
        if marker in lowered:
            return advice
    return "No known diagnosis. Check the server log for the underlying exception."


def _first_product_name(body: dict) -> str:
    """Pull the first product name out of a Store API listing response.

    The store-api serializes an EntitySearchResult's `elements` as a list, but a
    dict-of-values is tolerated rather than assuming one shape.
    """
    elements = (body or {}).get("elements")
    if isinstance(elements, dict):
        elements = list(elements.values())
    if isinstance(elements, list):
        for item in elements:
            if isinstance(item, dict) and (item.get("name") or "").strip():
                return item["name"].strip()
    return ""


def _product_id_from_search(text: str) -> str:
    """The first product id in a catalog-search result, or empty.

    Catalog-search wraps its payload as `{"data": {"products": [...]}}` and each
    product carries a UCP-native `id` — the value catalog-lookup's `ids` accepts.
    A malformed or empty result degrades to "" so the caller skips the lookup
    probe rather than sending a lookup nothing can resolve.
    """
    try:
        payload = json.loads(text).get("data", {})
    except (ValueError, TypeError, AttributeError):
        return ""
    products = payload.get("products") if isinstance(payload, dict) else None
    if isinstance(products, list) and products and isinstance(products[0], dict):
        return (products[0].get("id") or "").strip()
    return ""


def discover_store_query(default: str) -> tuple[str, str]:
    """A real product name to probe catalog-search with, or the default.

    Searching a made-up term is a legitimate empty result, not a failure — but a
    probe that searches a word the shop may not have proves less than one
    grounded in a product it does, and a zero-match code path is a candidate for
    the swallowed `internal` this suite keeps hitting. The store endpoint already
    authenticates with a sales-channel key, so use it to ask the Store API for
    one product name and probe with that.

    Returns (query, note). Every failure degrades to `default` with a note saying
    why, so this can only make the probe better, never block it.
    """
    if not mc.SW_SC_ACCESS_KEY:
        return default, f"no SW_SC_ACCESS_KEY, so probing with the default query {default!r}"
    url = f"{mc.SW_BASE_URL}{STORE_PRODUCT_ROUTE}"
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json", "sw-access-key": mc.SW_SC_ACCESS_KEY},
            json={"limit": 1, "includes": {"product": ["name"]}},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return default, f"Store API product listing failed ({type(exc).__name__}), probing with {default!r}"

    name = _first_product_name(body)
    if not name:
        return default, f"Store API returned no product, probing with {default!r}"
    return name, f"grounded in a real product: {name!r}"


def _execute(session_id: str, tool: str, args: dict, endpoint) -> tuple[str, str, float, dict]:
    """Run one probe. Returns (error, result_text, elapsed, raw_response).

    Timed, because duration separates two failures that look identical from the
    client: a request the server rejected outright, and one where the server
    blocked trying to fetch something. UCP fetches the profile URI mid-request,
    so if the shop's own URL is served by a worker pool with nothing spare, it
    waits on itself. Sub-second means rejected; multi-second means blocked.
    """
    prepared, _ = toolclass.prepare_call(tool, args)
    started = time.monotonic()
    response = mc.mcp_call(session_id, tool, prepared, endpoint)
    elapsed = time.monotonic() - started
    text = mc.mcp_result_text(response)
    # An in-band `{"success": false}` arrives as HTTP 200 with no transport
    # error, so checking only mcp_call_error would pass every rejected call.
    from eval.assertions import inband_error

    error = mc.mcp_call_error(response) or inband_error(text) or ""
    return error, text, elapsed, response


def _report_failure(endpoint_name: str, label: str, error: str, elapsed: float, text: str, response: dict) -> None:
    print(f"FAILED — {label} did not execute after {elapsed:.2f}s.\n  error: {error}\n  cause: {diagnose(error)}")

    # "Error while executing tool" is the MCP layer's generic wrapper, which it
    # uses whenever an exception escapes the tool rather than being returned
    # in-band. It carries none of the underlying message, so the diagnosis table
    # has nothing to match on and the run is a dead end. Two CI runs were spent
    # that way. Dump everything the response actually carries, so the next
    # occurrence is diagnosed from the log rather than from a local re-creation
    # that may not reproduce it.
    print("\n  --- raw response, for the cases the table cannot name ---")
    print(f"  result text: {(text or '(empty)')[:1000]}")
    print(f"  jsonrpc error: {json.dumps(response.get('error'), indent=2)[:1000]}")
    print(f"  full result: {json.dumps(response.get('result'), indent=2)[:1000]}")

    if endpoint_name == "store":
        print(profile_report(error))


def run(endpoint_name: str) -> int:
    endpoint = mc.endpoint_by_name(endpoint_name)
    session_id, _ = mc.mcp_init(endpoint)
    mc.enable_all_toolsets(session_id, endpoint)

    if endpoint_name != "store":
        tool, args = PROBES[endpoint_name]
        print(f"Preflight: {tool} on the {endpoint_name} endpoint ({endpoint.url})", flush=True)
        error, text, elapsed, response = _execute(session_id, tool, args, endpoint)
        if not error:
            print(f"OK — {tool} executed and returned a result ({len(text)} bytes) in {elapsed:.2f}s.")
            return 0
        _report_failure(endpoint_name, tool, error, elapsed, text, response)
        return 1

    import ucp

    search_tool, search_args = PROBES["store"]
    print(f"Preflight: {search_tool} on the store endpoint ({endpoint.url})", flush=True)
    print(f"  UCP-Agent: {ucp.agent_header(mc.SW_BASE_URL)}", flush=True)
    query, note = discover_store_query(search_args.get("query", "test"))
    print(f"  probe query: {query!r} — {note}", flush=True)

    failures = 0

    # 1) Search, grounded in a real product name, and keep the id it returns.
    error, text, elapsed, response = _execute(session_id, search_tool, {**search_args, "query": query}, endpoint)
    product_id = ""
    if error:
        failures += 1
        _report_failure("store", f"{search_tool} (query {query!r})", error, elapsed, text, response)
    else:
        product_id = _product_id_from_search(text)
        found = f"; first product id {product_id}." if product_id else " (no product in the result to look up)."
        print(f"OK — {search_tool} executed and returned a result ({len(text)} bytes) in {elapsed:.2f}s{found}")

    # 2) Look that product up by its real id — the tool search cannot stand in for.
    if product_id:
        error, text, elapsed, response = _execute(session_id, STORE_LOOKUP_TOOL, {"ids": product_id}, endpoint)
        if error:
            failures += 1
            _report_failure("store", f"{STORE_LOOKUP_TOOL} (ids {product_id})", error, elapsed, text, response)
        else:
            print(f"OK — {STORE_LOOKUP_TOOL} executed and returned a result ({len(text)} bytes) in {elapsed:.2f}s.")

    return 1 if failures else 0


def probe_profile(uri: str) -> tuple[int | None, str]:
    """Fetch the UCP profile URI and say whether it is one.

    Returns (status, verdict). A status alone is not enough: a Shopware error
    page is served as 200 with HTML, and 404 and "valid" are both plausible.
    """
    try:
        response = requests.get(uri, timeout=10)
    except requests.RequestException as exc:
        return None, f"unreachable ({type(exc).__name__})"
    if response.status_code != 200:
        return response.status_code, "not a profile — the server answered, but not with one"
    try:
        body = response.json()
    except ValueError:
        return 200, "200 but not JSON — almost certainly an error page"
    if not isinstance(body, dict) or "ucp" not in body:
        return 200, "200 and JSON, but no `ucp` key — not a UCP profile"
    return 200, "valid UCP profile"


def profile_report(error: str) -> str:
    """Resolve the one cause the error text can never name.

    `internal` means an exception escaped the tool, and the plugin reports that
    with no message and logs nothing — so from the outside it is indistinguishable
    from any other internal failure. Measured against a live lane, it is the
    profile fetch: a valid profile answers in ~0.16s, a 404 fails in ~0.19s with
    exactly this error, and CI failed in 0.14s.

    The server fetches this URI itself, mid-request, which is why the URI has to
    be one the SERVER can reach and why a published host:port is not automatically
    it.
    """
    import ucp

    uri = ucp.agent_header(mc.SW_BASE_URL).split('profile="', 1)[-1].rstrip('"')
    status, verdict = probe_profile(uri)
    lines = ["\n  --- the UCP profile the server has to fetch ---", f"  {uri} -> {status or 'no response'}: {verdict}"]
    if verdict != "valid UCP profile":
        lines.append(
            "  This is the cause. The server fetches that URI while handling the call, and a "
            "failure there escapes as a bare `internal`. Point UCP_PROFILE_URI at a URL the "
            "SERVER can reach that serves a real profile."
        )
    elif "internal" in error.lower():
        lines.append(
            "  The profile is fine, so `internal` is something else — and the plugin logs "
            "nothing for this path. The server log is the only place left to look."
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--endpoint", choices=sorted(PROBES), default="store")
    return run(parser.parse_args().endpoint)


if __name__ == "__main__":
    sys.exit(main())
