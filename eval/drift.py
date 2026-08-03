#!/usr/bin/env python3
"""Name what moved between two catalogue snapshots.

The drift step used to print a raw `git diff | head -200` of latest.json and
nothing else. That says drift happened without saying what drifted, so a red
nightly could not be told apart from the weaker model flapping — and 200 lines of
JSON diff is not something anyone reads at 6am.

This reduces the same two files to the list a reader acts on: which tools
appeared, disappeared, or had a description or schema change, plus whether the
default surface or the toolset taxonomy moved. Those last two matter more than any
description: a tool leaving the default surface, or a toolset being resliced,
changes what every fixture has to discover.

Usage:
    python -m eval.drift --old baseline.json --new tool-history/latest.json
    python -m eval.drift --old a.json --new b.json --format text
"""

import argparse
import json
import sys
from pathlib import Path
from typing import cast

from eval.result_schema import DriftSummary, Snapshot, Toolset, ToolsetChanges, as_object


def load(path: str) -> Snapshot:
    return cast(Snapshot, json.loads(Path(path).read_text()))


def summarise(old: Snapshot, new: Snapshot) -> DriftSummary:
    """Structured diff of two snapshots. Pure, so the rendering can be tested."""
    o = {t["name"]: t for t in old.get("tools", [])}
    n = {t["name"]: t for t in new.get("tools", [])}
    shared = sorted(set(o) & set(n))

    return DriftSummary(
        added=sorted(set(n) - set(o)),
        removed=sorted(set(o) - set(n)),
        described=[t for t in shared if o[t].get("description") != n[t].get("description")],
        schema=[t for t in shared if o[t].get("inputSchema") != n[t].get("inputSchema")],
        # The default surface is what a fresh session advertises. A change here is
        # categorically worse than a description edit: it moves what every fixture
        # has to discover before it can call anything.
        default_surface=old.get("default_tools") != new.get("default_tools"),
        toolsets=_toolset_changes(old, new),
        instructions=old.get("server_instructions") != new.get("server_instructions"),
    )


def _toolset_changes(old: Snapshot, new: Snapshot) -> ToolsetChanges:
    """Group-level changes, which reslice what the model has to enable."""
    o = {ts["name"]: ts for ts in old.get("toolsets", [])}
    n = {ts["name"]: ts for ts in new.get("toolsets", [])}

    def names(ts: Toolset) -> list[str]:
        # Tolerates both wire shapes: bare names, and the {name, title} pairs
        # shopware/shopware#18762 proposed.
        out: list[str] = []
        for t in ts.get("tools", []):
            out.append(str(as_object(t).get("name", "")) if isinstance(t, dict) else str(t))
        return sorted(out)

    return ToolsetChanges(
        added=sorted(set(n) - set(o)),
        removed=sorted(set(o) - set(n)),
        membership=[g for g in sorted(set(o) & set(n)) if names(o[g]) != names(n[g])],
    )


def is_significant(s: DriftSummary) -> bool:
    """Whether anything changed at all. Kept separate from rendering so the
    workflow can branch on it without parsing markdown."""
    ts = s["toolsets"]
    return bool(
        s["added"]
        or s["removed"]
        or s["described"]
        or s["schema"]
        or s["default_surface"]
        or s["instructions"]
        or ts["added"]
        or ts["removed"]
        or ts["membership"]
    )


def render(s: DriftSummary, heading: str = "Tool description drift") -> str:
    """Markdown, ordered by how much a reader should care."""
    if not is_significant(s):
        return "No catalogue drift vs the committed baseline.\n"

    out = [f"## {heading}", ""]

    # Structural first — these change what every fixture must discover.
    if s["default_surface"]:
        out.append("- **The default advertised surface changed.** Every fixture's discovery path is affected.")
    if s["toolsets"]["added"]:
        out.append(f"- **Toolsets added:** {_code(s['toolsets']['added'])}")
    if s["toolsets"]["removed"]:
        out.append(f"- **Toolsets removed:** {_code(s['toolsets']['removed'])} — fixtures pointing there will skip.")
    if s["toolsets"]["membership"]:
        out.append(f"- **Toolset membership changed:** {_code(s['toolsets']['membership'])}")
    if s["instructions"]:
        out.append("- **Server instructions changed** — this is part of the system prompt every run sends.")
    if s["added"]:
        out.append(f"- **Tools added ({len(s['added'])}):** {_code(s['added'])} — these need fixtures.")
    if s["removed"]:
        out.append(f"- **Tools removed ({len(s['removed'])}):** {_code(s['removed'])} — their fixtures will skip.")
    if s["schema"]:
        out.append(f"- **inputSchema changed ({len(s['schema'])}):** {_code(s['schema'])}")
    if s["described"]:
        out.append(f"- **Descriptions changed ({len(s['described'])}):** {_code(s['described'])}")

    out += [
        "",
        "Reconcile by updating `shopware.sha`, `tool-history/latest.json` and any affected",
        "eval expectations together, so the next run can attribute its own diff.",
        "",
    ]
    return "\n".join(out)


def _code(names: list[str]) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:12])
    return shown + (f" (+{len(names) - 12} more)" if len(names) > 12 else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old", required=True, help="baseline snapshot (e.g. git show HEAD:tool-history/latest.json)")
    parser.add_argument("--new", required=True, help="freshly generated snapshot")
    parser.add_argument("--heading", default="Tool description drift")
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="exit 1 when anything drifted, for use as a shell condition",
    )
    args = parser.parse_args()
    old_path = cast(str, args.old)
    new_path = cast(str, args.new)
    heading = cast(str, args.heading)
    use_exit_code = cast(bool, args.exit_code)

    try:
        summary = summarise(load(old_path), load(new_path))
    except (OSError, json.JSONDecodeError) as exc:
        # A missing or unreadable baseline is itself drift, but it must not crash
        # the workflow step that called this.
        print(f"::warning::Could not compare snapshots: {exc}", file=sys.stderr)
        print("Baseline missing or unreadable; treating as drift.\n")
        return 1 if use_exit_code else 0

    print(render(summary, heading))
    return 1 if (use_exit_code and is_significant(summary)) else 0


if __name__ == "__main__":
    sys.exit(main())
