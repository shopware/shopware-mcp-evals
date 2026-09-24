#!/usr/bin/env python3
"""Check our safety classification against what the server itself declares.

`bin/console debug:mcp --tools` prints the server-side registry: every tool, the
group it belongs to, and the ACL privileges it requires. That last column is the
useful one, because `toolclass.READ_ONLY` is a hand-maintained list and being
wrong about it is the one mistake with real consequences — a tool wrongly filed
there gets executed for real, with no dryRun, against whatever instance the suite
is pointed at.

Two independent sources agreeing is worth more than either alone:

  toolclass.py   a human decided this tool is safe to call
  debug:mcp      the server declares the privileges it actually needs

If a READ_ONLY tool needs `product:update`, one of the two is wrong, and it is
almost certainly ours.

Deliberately reads the console output rather than the protocol: this is about
what the server registered, which is a different question from what a client is
advertised after session setup and toolset enable (see eval/snapshot_tools.py for
that one, and eval/preflight.py for whether the thing can actually be called).

Both registries. Since shopware/shopware#18848 `debug:mcp` without `--scope`
prints one block per server — "Admin API (/api/_mcp)" and "Store API
(/store-api/_mcp)" — each with a `Tools (N) [<server>]` section and the same
table. An older Shopware prints one unlabelled admin table; it is still read,
as the admin registry, and a missing Store block is reported rather than failed.

Until this read the sections, it took every table row with enough columns and a
hyphenated name. On a Shopware with #18848 that mixed `shopware-store-api-context`
into the admin set and silently dropped every UCP tool (`create_cart`, ...),
whose names use underscores.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import toolclass

# Privilege verbs that mean "this tool changes something".
MUTATING_PRIVILEGES = ("create", "update", "delete", "write")

ADMIN = "Admin API"
STORE = "Store API"

# A row in the table debug:mcp prints. Columns are name, group, handler,
# dependencies, privileges — pipe-separated with padding. Underscores because
# the UCP tools are named by the spec (`create_cart`), not by Shopware.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]+$")

# `Tools (30) [Admin API]`, `Tools (3/30 allowed) [Admin API]`, or a bare
# `Tools (30)` from a Shopware that predates per-server sections.
_TOOLS_HEADING = re.compile(r"^Tools \(\d+(?:/\d+ allowed)?\)(?: \[(?P<scope>[^\]]+)\])?$")

# Any other section heading (`Prompts (4) [Admin API]`, `Resources (8) ...`):
# the end of a Tools table, so its rows are never read as tools.
_OTHER_HEADING = re.compile(r"^[A-Z][A-Za-z ]+ \(\d+[^)]*\)(?: \[[^\]]+\])?$")


def parse_tools(text: str) -> dict[str, dict[str, str]]:
    """Server -> tool name -> declared privileges, from `debug:mcp --tools --no-ansi`.

    Only rows inside a Tools section count. Returns an empty mapping for output
    with no Tools section in it, so a command that failed or printed a help page
    is a visible zero rather than a crash.
    """
    registries: dict[str, dict[str, str]] = {}
    scope: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if heading := _TOOLS_HEADING.match(line):
            named = cast(str | None, heading.group("scope"))
            scope = named or ADMIN
            _ = registries.setdefault(scope, {})
            continue
        if _OTHER_HEADING.match(line):
            scope = None
            continue
        if scope is None:
            continue
        columns = [part.strip() for part in line.split("|")]
        if len(columns) >= 6 and _TOOL_NAME.match(columns[1]):
            registries[scope][columns[1]] = columns[5]
    return registries


def mutates(privileges: str | None) -> bool:
    """`None` is a real registry value: a tool with no ACL at all, which is the
    common case for a reader rather than a finding."""
    return any(verb in (privileges or "").lower() for verb in MUTATING_PRIVILEGES)


def problems(tools: dict[str, str]) -> list[str]:
    """Every disagreement between the registry and toolclass, as readable lines."""
    found: list[str] = []
    for tool, privileges in sorted(tools.items()):
        classification = toolclass.classify(tool)
        if classification == "read_only" and mutates(privileges):
            found.append(
                f"{tool} is toolclass.READ_ONLY but the server requires '{privileges}'. "
                f"It would be executed for real, with no dryRun."
            )
        elif classification is None:
            found.append(
                f"{tool} is registered but unclassified in toolclass.py, so it can never be executed. "
                f"Declared privileges: '{privileges or 'none'}'."
            )
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--from-file",
        required=True,
        help="output of `bin/console debug:mcp --tools --no-ansi`, or - for stdin",
    )
    args = parser.parse_args(argv)

    from_file = cast(str, args.from_file)
    text = sys.stdin.read() if from_file == "-" else Path(from_file).read_text()
    registries = parse_tools(text)

    if not registries.get(ADMIN):
        print("FAILED — no admin tools parsed. Did `debug:mcp --tools --no-ansi` actually run?")
        return 1
    if STORE not in registries:
        # Not a failure: a Shopware before #18848 has no Store block to print.
        print(f"NOTE — no {STORE} section in the output, so only the admin registry was checked.")

    found = [f"[{scope}] {problem}" for scope, tools in registries.items() for problem in problems(tools)]
    counts = ", ".join(f"{len(tools)} {scope}" for scope, tools in registries.items())
    if not found:
        print(f"OK — {counts} tools registered, all classified, no read-only tool declares a mutating privilege.")
        return 0

    print(f"FAILED — {len(found)} disagreement(s) between the server registry and toolclass.py ({counts}):")
    for problem in found:
        print(f"  !! {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
