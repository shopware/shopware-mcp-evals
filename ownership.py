#!/usr/bin/env python3
"""Which codebase owns a tool, and how much a failure there costs.

The eval runs one MCP server, but the tools on it come from four repositories
with very different blast radius:

  core              shopware/shopware              ships to every merchant
  dev-tools         SwagMcpDevTools (bundle)       first-party, dev-only
  merchant-tools    SwagMcpMerchantTools (plugin)  optional
  agentic-commerce  shopware/agentic-commerce      optional, Store API / UCP

A single aggregate pass rate hides that. With 90 admin fixtures and one 90%
gate, nine core failures still read PASS as long as the plugins are clean —
which is exactly backwards, since the plugin numbers are the ones we can
afford to lose.

Attribution is by tool-name prefix, checked longest-first because
`shopware-ucp-*` is agentic-commerce, not core. This is a convention, so
tests/test_ownership.py enforces it against the committed tool snapshot: a
server-side tool that matches no prefix fails the unit tests rather than being
silently filed under core.

A fixture is attributed by its `expected_tool` — the description that should
have won is the one under test. A cross-boundary miss (core losing to a
merchant tool) is therefore counted against core, and `compare_runs.py` shows
what it lost to.
"""

# Ordered: first match wins, so more specific prefixes come first.
OWNER_PREFIXES = (
    ("shopware-ucp-", "agentic-commerce"),
    ("shopware-store-api-", "core"),
    ("swag-dev-tools-", "dev-tools"),
    ("merchant-", "merchant-tools"),
    ("shopware-", "core"),
)

CORE = "core"
UNKNOWN = "unattributed"

# The v2 discovery mechanism. Core owns these, and they are reported on their
# own line because a break here is categorically worse than one tool being
# mis-picked: nothing else on the server can be found at all. They still count
# inside core's denominator for gating — nine fixtures is too few to gate on
# (a single failure moves the rate 11 points).
DISCOVERY_TOOLS = frozenset(
    {
        "shopware-tool-search",
        "shopware-toolset-enable",
        "shopware-toolsets-list",
    }
)
DISCOVERY = "core · discovery"

# Display order, most critical first. Anything unmapped sorts last and is meant
# to be noticed.
TIER_ORDER = (DISCOVERY, "core", "dev-tools", "merchant-tools", "agentic-commerce", UNKNOWN)

# Optional plugins: reported, never gated on their own. Kept as data so the
# workflow and the summary agree on what "optional" means.
OPTIONAL = frozenset({"merchant-tools", "agentic-commerce"})


def owner_of(tool: str) -> str:
    """Owning repository for a tool name, or UNKNOWN if no prefix matches."""
    for prefix, owner in OWNER_PREFIXES:
        if tool.startswith(prefix):
            return owner
    return UNKNOWN


def tier_of(tool: str) -> str:
    """Reporting bucket: like owner_of, but splits core's discovery meta-tools out."""
    if tool in DISCOVERY_TOOLS:
        return DISCOVERY
    return owner_of(tool)


def breakdown(results: list[dict]) -> dict[str, dict]:
    """Per-tier pass counts over the results given.

    Callers pass an already-filtered list (scored, non-errored) — this only
    groups, so the same exclusions that apply to the overall rate apply here
    and the numbers stay comparable.

    `ids` carries every fixture this tier graded, not just the failures, because
    the consumer that merges several runs needs it. `eval/summary.py` sums these
    buckets across the admin primary, the second validator and the Store suite;
    with counts alone it could only add `total`, which double-counts the two
    admin runs over one fixture set and reports dev-tools out of 42 when there
    are 21 dev-tools fixtures. The id lists let it union instead of add.
    """
    out: dict[str, dict] = {}
    for r in results or []:
        # A negative fixture expects no tool, so no repository owns it — it is a
        # statement about the catalogue as a whole. Attributing it would file
        # every one of them under "unattributed" and invent a tier that reads
        # like a drift alarm.
        if not r.get("expected_tool"):
            continue
        tier = tier_of(r["expected_tool"])
        bucket = out.setdefault(tier, {"passed": 0, "total": 0, "ids": [], "failed_ids": []})
        bucket["total"] += 1
        bucket["ids"].append(r.get("id"))
        if r.get("passed"):
            bucket["passed"] += 1
        else:
            bucket["failed_ids"].append(r.get("id"))
    for bucket in out.values():
        bucket["rate"] = bucket["passed"] / bucket["total"] if bucket["total"] else 0.0
    return {t: out[t] for t in TIER_ORDER if t in out} | {t: v for t, v in out.items() if t not in TIER_ORDER}


def core_rate(results: list[dict]) -> tuple[int, int, float]:
    """Passed, total and rate over core fixtures — discovery meta-tools included.

    This is the number that gates separately. Rolling discovery in keeps the
    denominator at a size where one nondeterministic miss doesn't flip the
    build, while `breakdown` still reports the two lines apart.
    """
    # Negative fixtures are excluded for the same reason as in breakdown: they
    # name no tool, so they cannot be charged to core's denominator. They still
    # count in the overall rate, which is the axis they belong on.
    core = [r for r in results or [] if r.get("expected_tool") and owner_of(r["expected_tool"]) == CORE]
    passed = sum(1 for r in core if r.get("passed"))
    return passed, len(core), (passed / len(core) if core else 1.0)
