#!/usr/bin/env python3
"""
Snapshot the live MCP server's tool list.

Writes a normalized JSON document (sorted by tool name) so a `git diff` between
two snapshots surfaces description / schema churn directly.

Usage:
    python eval/snapshot_tools.py --output tool-history/latest.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

BASE = Path(__file__).parent.parent


def load_env() -> None:
    env_file = BASE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def fetch_tools(base_url: str, access_key: str, secret_key: str) -> list[dict]:
    auth = {
        "sw-access-key": access_key,
        "sw-secret-access-key": secret_key,
        "Content-Type": "application/json",
    }

    init = requests.post(
        f"{base_url}/api/_mcp",
        headers=auth,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-eval-snapshot", "version": "1.0"},
            },
            "id": 1,
        },
        timeout=30,
    )
    init.raise_for_status()

    session_id = init.headers.get("Mcp-Session-Id", "")
    if not session_id:
        raise RuntimeError("No Mcp-Session-Id in initialize response")

    server_instructions = init.json().get("result", {}).get("instructions", "")

    listing = requests.post(
        f"{base_url}/api/_mcp",
        headers={**auth, "Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2},
        timeout=30,
    )
    listing.raise_for_status()
    tools = listing.json().get("result", {}).get("tools", [])

    return server_instructions, tools


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(BASE / "tool-history" / "latest.json"),
        help="Output file path",
    )
    args = parser.parse_args()

    load_env()
    base_url = os.environ.get("SW_BASE_URL", "").rstrip("/")
    access_key = os.environ.get("SW_ACCESS_KEY", "")
    secret_key = os.environ.get("SW_SECRET_ACCESS_KEY", "")

    if not (base_url and access_key and secret_key):
        print("ERROR: SW_BASE_URL, SW_ACCESS_KEY, SW_SECRET_ACCESS_KEY required", file=sys.stderr)
        return 1

    instructions, tools = fetch_tools(base_url, access_key, secret_key)

    normalized = {
        "server_instructions": instructions,
        "tools": sorted(
            (
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {}),
                }
                for t in tools
            ),
            key=lambda t: t["name"],
        ),
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {out} ({len(normalized['tools'])} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
