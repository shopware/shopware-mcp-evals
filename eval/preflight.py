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

import mcp_client as mc
import toolclass

# A read-only tool per endpoint whose success proves the whole chain works:
# session, toolset enable, argument validation, and — on store — the UCP-Agent
# header, its profile fetch, and both allowlists.
PROBES = {
    "store": ("shopware-ucp-catalog-search", {"query": "test"}),
    "admin": ("shopware-entity-search", {"entity": "product", "limit": "1"}),
}

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


def run(endpoint_name: str) -> int:
    tool, args = PROBES[endpoint_name]
    endpoint = mc.endpoint_by_name(endpoint_name)
    print(f"Preflight: {tool} on the {endpoint_name} endpoint ({endpoint.url})", flush=True)
    if endpoint_name == "store":
        import ucp

        print(f"  UCP-Agent: {ucp.agent_header(mc.SW_BASE_URL)}", flush=True)

    session_id, _ = mc.mcp_init(endpoint)
    mc.enable_all_toolsets(session_id, endpoint)

    prepared, _ = toolclass.prepare_call(tool, args)
    # Timed, because duration separates two failures that look identical from the
    # client: a request the server rejected outright, and one where the server
    # blocked trying to fetch something. UCP fetches the profile URI mid-request,
    # so if the shop's own URL is served by a worker pool with nothing spare, it
    # waits on itself. Sub-second means rejected; multi-second means blocked.
    started = time.monotonic()
    response = mc.mcp_call(session_id, tool, prepared, endpoint)
    elapsed = time.monotonic() - started
    text = mc.mcp_result_text(response)

    # An in-band `{"success": false}` arrives as HTTP 200 with no transport
    # error, so checking only mcp_call_error would pass every rejected call.
    from eval.assertions import inband_error

    error = mc.mcp_call_error(response) or inband_error(text) or ""

    if not error:
        print(f"OK — {tool} executed and returned a result ({len(text)} bytes) in {elapsed:.2f}s.")
        return 0

    print(f"FAILED — {tool} did not execute after {elapsed:.2f}s.\n  error: {error}\n  cause: {diagnose(error)}")

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
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", choices=sorted(PROBES), default="store")
    return run(parser.parse_args().endpoint)


if __name__ == "__main__":
    sys.exit(main())
