#!/usr/bin/env python3
"""
Shared MCP HTTP helpers for the Shopware MCP servers.

Two endpoints, same discovery machinery:
  - ADMIN (/api/_mcp)      auth: integration access key + secret
  - STORE (/store-api/_mcp) auth: sales-channel access key + context token

Used by eval/run.py, eval/snapshot_tools.py, and functional/run.py. Speaks
JSON-RPC 2.0 over HTTP
POST, tracking the Mcp-Session-Id header. Every function takes an optional
`endpoint` (default ADMIN) so existing admin call sites are unchanged.

MCP Server v2 notes:
  - tools/list returns only the advertised surface (meta-tools + toolsets
    enabled for this session) and is cursor-paginated.
  - shopware-toolset-enable grows the advertised surface per session.
  - Deferred tools stay callable directly; the allowlist is the call boundary.
"""

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

import requests

import ucp
from ownership import owner_of

# The MCP endpoint throttles bursts (HTTP 429). Retry a bounded number of times,
# honoring the server's advertised wait, so a functional run pacing ~100 calls
# does not fail on transient throttling.
THROTTLE_MAX_RETRIES = 5
THROTTLE_MAX_WAIT_S = 20.0

BASE = Path(__file__).resolve().parent

# Meta-tools of the v2 discovery layer. Always advertised on both endpoints.
META_TOOLS = {
    "shopware-tool-search",
    "shopware-toolsets-list",
    "shopware-toolset-enable",
}

# The default advertised surface. Every non-meta tool is deferred, so a fresh
# session sees only the meta-tools — the model must discover and enable
# everything else. Same on the admin and Store API endpoints.
DEFAULT_SURFACE = set(META_TOOLS)


def load_env() -> None:
    env_file = BASE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


load_env()

SW_BASE_URL = os.environ.get("SW_BASE_URL", "http://localhost:8000").rstrip("/")
SW_ACCESS_KEY = os.environ.get("SW_ACCESS_KEY", "")
SW_SECRET_ACCESS_KEY = os.environ.get("SW_SECRET_ACCESS_KEY", "")
# Sales-channel access key for the Store API endpoint (sw-access-key there is a
# sales-channel key, not an integration key).
SW_SC_ACCESS_KEY = os.environ.get("SW_SC_ACCESS_KEY", "")


class Endpoint:
    """An MCP HTTP endpoint: its URL plus the base auth headers to send.

    `base_url` is a parameter rather than a read of the module-level
    SW_BASE_URL so an endpoint can be built for a server this process was not
    configured for — a test against a local fake, or two instances in one run.
    It defaults to the configured value, which is what every real caller wants.
    """

    def __init__(self, name: str, path: str, auth_headers: dict, base_url: str | None = None):
        self.name = name
        self.url = f"{(base_url or SW_BASE_URL).rstrip('/')}{path}"
        self.auth_headers = {"Content-Type": "application/json", **auth_headers}


def admin_endpoint(
    access_key: str | None = None, secret_access_key: str | None = None, base_url: str | None = None
) -> Endpoint:
    """Build an admin endpoint, defaulting to the process configuration."""
    return Endpoint(
        "admin",
        "/api/_mcp",
        {
            "sw-access-key": access_key if access_key is not None else SW_ACCESS_KEY,
            "sw-secret-access-key": secret_access_key if secret_access_key is not None else SW_SECRET_ACCESS_KEY,
        },
        base_url,
    )


def store_endpoint(
    access_key: str | None = None,
    context_token: str | None = None,
    base_url: str | None = None,
    profile_uri: str | None = None,
) -> Endpoint:
    """Build a Store API endpoint, defaulting to the process configuration.

    The context token is generated per endpoint rather than per request so
    cart/checkout state persists across calls — the server would otherwise issue
    a fresh one each time. Pass one explicitly to resume a known cart, or to keep
    a test deterministic.
    """
    return Endpoint(
        "store",
        "/store-api/_mcp",
        {
            "sw-access-key": access_key if access_key is not None else SW_SC_ACCESS_KEY,
            "sw-context-token": context_token or secrets.token_hex(16),
            # UCP-specific, and deliberately the only UCP reference in this
            # module — see ucp.py for why it is isolated.
            "UCP-Agent": ucp.agent_header(base_url or SW_BASE_URL, profile_uri),
        },
        base_url,
    )


# The process-wide defaults every entry point uses. Built here rather than lazily
# because the Store token must be stable for the lifetime of the run: two
# `store_endpoint()` calls produce two carts, and a runner that rebuilt its
# endpoint mid-run would silently lose the one it had been filling.
ADMIN = admin_endpoint()
STORE = store_endpoint()

# Backwards-compatible admin aliases (referenced by older call sites).
MCP_URL = ADMIN.url
AUTH_HEADERS = ADMIN.auth_headers


def _throttle_wait(resp: requests.Response) -> float:
    """Seconds to wait before retrying a 429, from Retry-After or the server's
    'throttled for N seconds' hint, capped so a run can never stall for long."""
    retry_after = resp.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return min(float(retry_after), THROTTLE_MAX_WAIT_S)
    try:
        match = re.search(r"(\d+)\s*second", resp.json().get("error", {}).get("message", ""))
        if match:
            return min(float(match.group(1)), THROTTLE_MAX_WAIT_S)
    except (ValueError, TypeError):
        pass
    return 5.0


def _rpc(
    method: str,
    params: dict,
    session_id: str | None = None,
    rpc_id: int = 1,
    endpoint: Endpoint = ADMIN,
    extra_headers: dict | None = None,
) -> requests.Response:
    headers = {**endpoint.auth_headers, **(extra_headers or {})}
    # Streamable HTTP requires the client to accept both reply shapes; the server
    # answers application/json for a lone response and text/event-stream when it
    # also has to push a notification (e.g. tools/list_changed after an enable).
    headers["Accept"] = "application/json, text/event-stream"
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id}
    resp: requests.Response | None = None
    for attempt in range(THROTTLE_MAX_RETRIES + 1):
        resp = requests.post(endpoint.url, headers=headers, json=body, timeout=30)
        if resp.status_code == 429 and attempt < THROTTLE_MAX_RETRIES:
            time.sleep(_throttle_wait(resp))
            continue
        break
    # range(THROTTLE_MAX_RETRIES + 1) is never empty, so the loop always assigns
    # resp at least once; the assert states that invariant for the type checker.
    assert resp is not None
    resp.raise_for_status()
    return resp


def _parse_sse(text: str) -> list[dict]:
    """Parse an SSE body into its JSON-RPC messages (the `data:` payloads)."""
    messages: list[dict] = []
    data: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data.append(line[len("data:") :].lstrip())
        elif not line and data:  # a blank line terminates an event
            try:
                messages.append(json.loads("\n".join(data)))
            except ValueError:
                pass
            data = []
    if data:  # trailing event with no terminating blank line
        try:
            messages.append(json.loads("\n".join(data)))
        except ValueError:
            pass
    return messages


def _pick(messages: list, rpc_id: int) -> dict:
    """Return the JSON-RPC response whose id matches rpc_id; server-initiated
    notifications (which carry no id) are ignored. {} if none match."""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("id") == rpc_id:
            return msg
    return {}


def _response(resp: requests.Response, rpc_id: int) -> dict:
    """Extract the JSON-RPC response from an MCP reply, handling both Content-Types
    the Streamable HTTP transport allows: a single JSON object (application/json)
    or an SSE stream (text/event-stream) carrying the response plus any server
    notifications such as tools/list_changed. A top-level JSON array is tolerated
    defensively (a spec-removed batch shape)."""
    if "text/event-stream" in resp.headers.get("Content-Type", ""):
        return _pick(_parse_sse(resp.text), rpc_id)
    payload = resp.json()
    if isinstance(payload, list):
        return _pick(payload, rpc_id)
    return payload if isinstance(payload, dict) else {}


def _rpc_json(
    method: str,
    params: dict,
    session_id: str | None = None,
    rpc_id: int = 1,
    endpoint: Endpoint = ADMIN,
    extra_headers: dict | None = None,
) -> dict:
    """_rpc plus response extraction (single JSON object or SSE stream)."""
    return _response(_rpc(method, params, session_id, rpc_id, endpoint, extra_headers), rpc_id)


def mcp_init(endpoint: Endpoint = ADMIN) -> tuple[str, str]:
    """Initialize MCP session. Returns (session_id, server_instructions)."""
    resp = _rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-eval", "version": "2.0"},
        },
        endpoint=endpoint,
    )
    session_id = resp.headers.get("Mcp-Session-Id", "")
    if not session_id:
        raise RuntimeError("No Mcp-Session-Id in response headers")
    instructions = _response(resp, 1).get("result", {}).get("instructions", "")
    return session_id, instructions


def mcp_call(session_id: str, tool: str, arguments: dict, endpoint: Endpoint = ADMIN) -> dict:
    """Call a tool. Returns the full JSON-RPC response dict."""
    return _rpc_json(
        "tools/call",
        {"name": tool, "arguments": arguments},
        session_id,
        rpc_id=99,
        endpoint=endpoint,
        # UCP-specific, and the only other UCP reference in this module — see
        # ucp.py. Empty for every non-UCP tool, so the admin path is untouched.
        extra_headers=ucp.call_headers(tool),
    )


def mcp_result_text(resp: dict) -> str:
    """Extract the first text content block from a tools/call response."""
    content = resp.get("result", {}).get("content", [])
    for block in content:
        if isinstance(block, dict) and block.get("type", "text") == "text":
            return block.get("text", "")
    return ""


def mcp_result_meta(resp: dict) -> dict:
    return resp.get("result", {}).get("_meta", {}) or {}


def mcp_call_error(resp: dict) -> str:
    """Return the error message of a tools/call response ('' if none).

    Covers both protocol-level errors (error.message) and tool-level errors
    (result.isError with the message in the text content).
    """
    if "error" in resp:
        return resp["error"].get("message", "unknown error")
    if resp.get("result", {}).get("isError"):
        return mcp_result_text(resp) or "tool error"
    return ""


def mcp_tools_list_all(session_id: str, endpoint: Endpoint = ADMIN) -> list[dict]:
    """Fetch the full advertised tools/list for this session, following
    nextCursor pagination. Raises on duplicate tool names across pages
    (that would be a server-side pagination bug)."""
    tools: list[dict] = []
    seen: set[str] = set()
    cursor = None
    for _ in range(50):  # runaway guard
        params = {} if cursor is None else {"cursor": cursor}
        result = _rpc_json("tools/list", params, session_id, rpc_id=2, endpoint=endpoint).get("result", {})
        for t in result.get("tools", []):
            name = t.get("name", "")
            if name in seen:
                raise RuntimeError(f"Duplicate tool '{name}' across tools/list pages")
            seen.add(name)
            tools.append(t)
        cursor = result.get("nextCursor")
        if not cursor:
            return tools
    raise RuntimeError("tools/list pagination did not terminate within 50 pages")


def mcp_toolsets_list(session_id: str, endpoint: Endpoint = ADMIN) -> list[dict]:
    """Call shopware-toolsets-list and return the parsed toolsets array:
    [{name, title, description, tools, enabled}, ...]"""
    resp = mcp_call(session_id, "shopware-toolsets-list", {}, endpoint=endpoint)
    err = mcp_call_error(resp)
    if err:
        raise RuntimeError(f"shopware-toolsets-list failed: {err}")
    payload = json.loads(mcp_result_text(resp))
    return payload.get("data", {}).get("toolsets", [])


def enable_toolset(session_id: str, toolset: str, endpoint: Endpoint = ADMIN) -> dict:
    """Enable one toolset for this session. Returns the tools/call response."""
    return mcp_call(session_id, "shopware-toolset-enable", {"toolset": toolset}, endpoint=endpoint)


def enable_all_toolsets(session_id: str, endpoint: Endpoint = ADMIN) -> list[str]:
    """Enable every not-yet-enabled toolset for this session.
    Returns the names of all toolsets enabled afterwards."""
    enabled = []
    for toolset in mcp_toolsets_list(session_id, endpoint=endpoint):
        if not toolset.get("enabled"):
            resp = enable_toolset(session_id, toolset["name"], endpoint=endpoint)
            err = mcp_call_error(resp)
            if err:
                raise RuntimeError(f"Failed to enable toolset '{toolset['name']}': {err}")
        enabled.append(toolset["name"])
    return enabled


def mcp_fetch_system_prompt(session_id: str, server_instructions: str, endpoint: Endpoint = ADMIN) -> str:
    """Fetch all MCP context prompts and combine with server instructions.

    Prompts are optional in MCP, and an endpoint that does not serve them is not
    a broken endpoint. A 404 here used to abort the whole suite before a single
    fixture ran — the run reported a crash, not a result, over a feature the
    Store endpoint answers with an empty list anyway. Degrade to the server
    instructions instead, and say so, so a genuinely missing prompt set is
    visible without being fatal.
    """
    return mcp_fetch_context_prompts(session_id, server_instructions, endpoint=endpoint)[0]


def mcp_fetch_context_prompts(
    session_id: str, server_instructions: str, endpoint: Endpoint = ADMIN, owners: frozenset[str] | None = None
) -> tuple[str, dict]:
    """The context prompt, plus an inventory of what went into it.

    The inventory is the point. The two endpoints are not comparable and nothing
    said so: admin serves four prompts totalling ~20k characters — a guide naming
    every tool and its parameters — while store serves none at all and gets ~460
    characters of server instructions. A pass rate from each was being read side
    by side as if they were the same measurement.

    Returns (prompt_text, {"names": [...], "chars": {name: n}, "total_chars": n,
    "sha256": "..."}). The digest is there so two runs can be told apart when the
    prompt *content* changes — a boolean cannot.
    """
    inventory: dict = {
        "names": [],
        "chars": {},
        "instructions_chars": len(server_instructions or ""),
        # What the server offered, as distinct from what we took. Without this a
        # narrowed run is indistinguishable from an endpoint that ships nothing —
        # and the store endpoint really does ship nothing.
        "available": [],
        "excluded": [],
    }
    try:
        result = _rpc_json("prompts/list", {}, session_id, rpc_id=3, endpoint=endpoint).get("result", {})
    except requests.HTTPError as exc:
        print(f"WARNING: {endpoint.name} endpoint does not serve prompts/list ({exc}); using server instructions only")
        text = server_instructions.strip()
        inventory |= {"total_chars": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()[:12]}
        return text, inventory
    prompt_names = [p["name"] for p in result.get("prompts", [])]

    parts = []
    if server_instructions:
        parts.append(server_instructions.strip())

    inventory["available"] = list(prompt_names)
    for name in prompt_names:
        # Attribution by the same prefix rule that maps tools to owners, so a
        # prompt and the tools it describes can never end up in different areas.
        if owners is not None and owner_of(name) not in owners:
            inventory["excluded"].append(name)
            continue
        result = _rpc_json("prompts/get", {"name": name}, session_id, rpc_id=4, endpoint=endpoint).get("result", {})
        messages = result.get("messages", [])
        collected = 0
        for msg in messages:
            content = msg.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            if text.strip():
                parts.append(text.strip())
                collected += len(text.strip())
        inventory["names"].append(name)
        inventory["chars"][name] = collected

    prompt = "\n\n---\n\n".join(parts)
    inventory |= {"total_chars": len(prompt), "sha256": hashlib.sha256(prompt.encode()).hexdigest()[:12]}
    return prompt, inventory


def endpoint_by_name(name: str) -> Endpoint:
    return {"admin": ADMIN, "store": STORE}[name]
