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

Scope: the admin registry. `debug:mcp` takes no endpoint flag and does not list
the Store/UCP tools at all — measured, not assumed: it reports 30 tools on an
instance whose store endpoint advertises 17 more.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import cast

import toolclass

# Privilege verbs that mean "this tool changes something".
MUTATING_PRIVILEGES = ("create", "update", "delete", "write")

# A row in the table debug:mcp prints. Columns are name, group, source,
# dependencies, privileges — pipe-separated with padding.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9-]+$")


def parse_tools(text: str) -> dict[str, str]:
    """Tool name -> declared privileges, from `debug:mcp --tools --no-ansi`.

    Returns an empty mapping for output with no table in it, so a command that
    failed or printed a help page is a visible zero rather than a crash.
    """
    tools: dict[str, str] = {}
    for line in text.splitlines():
        columns = [part.strip() for part in line.split("|")]
        if len(columns) >= 6 and _TOOL_NAME.match(columns[1]):
            tools[columns[1]] = columns[5]
    return tools


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--from-file",
        required=True,
        help="output of `bin/console debug:mcp --tools --no-ansi`, or - for stdin",
    )
    args = parser.parse_args()

    from_file = cast(str, args.from_file)
    text = sys.stdin.read() if from_file == "-" else Path(from_file).read_text()
    tools = parse_tools(text)

    if not tools:
        print("FAILED — no tools parsed. Did `debug:mcp --tools --no-ansi` actually run?")
        return 1

    found = problems(tools)
    if not found:
        print(f"OK — {len(tools)} registered tools, all classified, no read-only tool declares a mutating privilege.")
        return 0

    print(f"FAILED — {len(found)} disagreement(s) between the server registry and toolclass.py:")
    for problem in found:
        print(f"  !! {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
