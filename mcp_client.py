#!/usr/bin/env python3
"""
Shared MCP HTTP helpers for the Shopware MCP servers.

Two endpoints, same discovery machinery:
  - ADMIN (/api/_mcp)      auth: integration access key + secret
  - STORE (/store-api/_mcp) auth: sales-channel access key + context token

Used by eval/run.py and eval/snapshot_tools.py. Speaks JSON-RPC 2.0 over HTTP
POST, tracking the Mcp-Session-Id header. Every function takes an optional
`endpoint` (default ADMIN) so existing admin call sites are unchanged.

MCP Server v2 notes:
  - tools/list returns only the advertised surface (meta-tools + toolsets
    enabled for this session) and is cursor-paginated.
  - shopware-toolset-enable grows the advertised surface per session.
  - Deferred tools stay callable directly; the allowlist is the call boundary.
"""

import json
import os
import secrets
from pathlib import Path

import requests

BASE = Path(__file__).parent.parent

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
    """An MCP HTTP endpoint: its URL plus the base auth headers to send."""

    def __init__(self, name: str, path: str, auth_headers: dict):
        self.name = name
        self.url = f"{SW_BASE_URL}{path}"
        self.auth_headers = {"Content-Type": "application/json", **auth_headers}


ADMIN = Endpoint("admin", "/api/_mcp", {
    "sw-access-key": SW_ACCESS_KEY,
    "sw-secret-access-key": SW_SECRET_ACCESS_KEY,
})

# The store endpoint carries a fixed context token for the whole run so
# cart/checkout state persists across calls (the server would otherwise issue a
# fresh one each request).
STORE = Endpoint("store", "/store-api/_mcp", {
    "sw-access-key": SW_SC_ACCESS_KEY,
    "sw-context-token": secrets.token_hex(16),
})

# Backwards-compatible admin aliases (referenced by older call sites).
MCP_URL = ADMIN.url
AUTH_HEADERS = ADMIN.auth_headers


def _rpc(method: str, params: dict, session_id: str | None = None,
         rpc_id: int = 1, endpoint: Endpoint = ADMIN) -> requests.Response:
    headers = dict(endpoint.auth_headers)
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    resp = requests.post(
        endpoint.url,
        headers=headers,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp


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
    instructions = resp.json().get("result", {}).get("instructions", "")
    return session_id, instructions


def mcp_call(session_id: str, tool: str, arguments: dict, endpoint: Endpoint = ADMIN) -> dict:
    """Call a tool. Returns the full JSON-RPC response dict."""
    return _rpc("tools/call", {"name": tool, "arguments": arguments},
                session_id, rpc_id=99, endpoint=endpoint).json()


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
        result = _rpc("tools/list", params, session_id, rpc_id=2, endpoint=endpoint).json().get("result", {})
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
    """Fetch all MCP context prompts and combine with server instructions."""
    resp = _rpc("prompts/list", {}, session_id, rpc_id=3, endpoint=endpoint)
    prompt_names = [p["name"] for p in resp.json().get("result", {}).get("prompts", [])]

    parts = []
    if server_instructions:
        parts.append(server_instructions.strip())

    for name in prompt_names:
        resp = _rpc("prompts/get", {"name": name}, session_id, rpc_id=4, endpoint=endpoint)
        messages = resp.json().get("result", {}).get("messages", [])
        for msg in messages:
            content = msg.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            if text.strip():
                parts.append(text.strip())

    return "\n\n---\n\n".join(parts)


def endpoint_by_name(name: str) -> Endpoint:
    return {"admin": ADMIN, "store": STORE}[name]
