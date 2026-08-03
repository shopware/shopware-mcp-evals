#!/usr/bin/env python3
"""
Snapshot the live MCP server's full tool catalogue.

MCP Server v2 advertises only a small default surface on a fresh session; the
rest of the catalogue hides behind toolsets. The snapshot therefore captures:

  - default_tools:  tool names advertised on a fresh session (paginated walk)
  - toolsets:       the toolset taxonomy (name, title, description, tools)
  - tools:          full definitions of the whole catalogue, taken after
                    enabling every toolset for the snapshot session

Everything is normalized and sorted so a `git diff` between two snapshots
surfaces default-surface changes, toolset membership changes, and
description/schema churn directly.

Usage:
    python -m eval.snapshot_tools --output tool-history/latest.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import cast

from eval.result_schema import Snapshot, ToolDef, Toolset
from mcp_client import (
    BASE,
    SW_ACCESS_KEY,
    SW_BASE_URL,
    SW_SC_ACCESS_KEY,
    SW_SECRET_ACCESS_KEY,
    enable_all_toolsets,
    endpoint_by_name,
    mcp_init,
    mcp_tools_list_all,
    mcp_toolsets_list,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(BASE / "tool-history" / "latest.json"),
        help="Output file path",
    )
    # Without this the Store catalogue was never snapshotted, and everything
    # that derives from a snapshot had to be guessed for it: toolclass could not
    # read schemas to find dryRun, tests/test_fixtures.py could not check
    # expected_toolset, and drift detection did not cover it at all. The Store
    # fixtures ended up declaring toolsets ('shopware', 'store-api') that do not
    # exist — the real ones are shopware-ucp-cart, -catalog, -checkout — which
    # nothing could catch.
    parser.add_argument(
        "--endpoint",
        default="admin",
        choices=["admin", "store"],
        help="Which MCP endpoint to snapshot (default admin)",
    )
    args = parser.parse_args()

    endpoint = endpoint_by_name(cast(str, args.endpoint))
    if cast(str, args.endpoint) == "store":
        missing = [n for n, v in (("SW_BASE_URL", SW_BASE_URL), ("SW_SC_ACCESS_KEY", SW_SC_ACCESS_KEY)) if not v]
    else:
        missing = [
            n
            for n, v in (
                ("SW_BASE_URL", SW_BASE_URL),
                ("SW_ACCESS_KEY", SW_ACCESS_KEY),
                ("SW_SECRET_ACCESS_KEY", SW_SECRET_ACCESS_KEY),
            )
            if not v
        ]
    if missing:
        print(f"ERROR: {', '.join(missing)} required", file=sys.stderr)
        return 1

    session_id, instructions = mcp_init(endpoint=endpoint)

    default_tools = sorted(t.get("name", "") for t in mcp_tools_list_all(session_id, endpoint=endpoint))

    toolsets = sorted(
        (
            Toolset(
                name=ts.get("name", ""),
                title=ts.get("title", ""),
                description=ts.get("description", ""),
                # 'enabled' is session state, not catalogue shape — drop it.
                tools=sorted(ts.get("tools", [])),
            )
            for ts in mcp_toolsets_list(session_id, endpoint=endpoint)
        ),
        key=lambda ts: ts["name"],
    )

    enable_all_toolsets(session_id, endpoint=endpoint)
    full_catalogue = mcp_tools_list_all(session_id, endpoint=endpoint)

    normalized = Snapshot(
        server_instructions=instructions,
        default_tools=default_tools,
        toolsets=toolsets,
        tools=sorted(
            (
                ToolDef(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    inputSchema=t.get("inputSchema", {}),
                )
                for t in full_catalogue
            ),
            key=lambda t: t["name"],
        ),
    )

    out = Path(cast(str, args.output))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    print(
        f"Wrote {out} ({len(default_tools)} default tools, "
        f"{len(toolsets)} toolsets, {len(normalized['tools'])} tools total)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
