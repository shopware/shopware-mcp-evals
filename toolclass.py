#!/usr/bin/env python3
"""Whether a tool can be executed during an eval, and how to make it safe.

Grading a selection without executing it says nothing about whether the call
would have worked: a tool named correctly with nonsense arguments scores the
same as one that runs. Executing means sometimes executing a *wrong* selection,
so the safety boundary has to be mechanical rather than a per-fixture judgement.

Three classes, derived from the schemas rather than invented:

  READ_ONLY     execute as-is.
  DRY_RUNNABLE  execute with dryRun forced true. These are exactly the tools
                whose inputSchema declares a `dryRun` property — the server
                itself is saying "this mutates, here is the safe path".
  UNSAFE        never execute. Tools that mutate and offer no dryRun, so there
                is no safe way to call them. They keep the old selection-only
                grading; there is no way to give them more without touching
                real state on whatever instance the suite is pointed at, and
                that includes a developer's own shop.

The third class is the one worth knowing about. On the admin endpoint it is
three of thirty — `shopware-media-upload`, `merchant-cart-manage` and
`swag-dev-tools-scaffold` all change state with no opt-out, so they cannot
participate in result assertions or in the recovery loop. Adding `dryRun` to
them server-side is what would fix that.

The Store endpoint used to be almost entirely unsafe for a different reason —
there was no snapshot to read schemas from, so anything that might mutate was
guessed unsafe. The plugin has since added dryRun to exactly its mutating tools,
so that guess is gone and only the three admin tools above remain.

A tool in none of the three sets is not executed either — an unclassified tool
is loud (tests/test_toolclass.py fails on it) rather than quietly assumed safe.

The agentic-commerce plugin's own tools are classified in `ucp.py` and merged in
below, so that plugin can be dropped by deleting one file rather than picking
its entries out of three sets here.
"""

import ucp

# The server declares these by exposing a `dryRun` property. Kept as an explicit
# list rather than read from the snapshot at runtime so the safety boundary is
# reviewable in a diff, and cross-checked against the snapshot in the tests.
DRY_RUNNABLE = frozenset(
    {
        "merchant-cart-checkout",
        "merchant-product-create",
        "shopware-entity-delete",
        "shopware-entity-upsert",
        "shopware-order-state",
        "shopware-system-config-write",
        "shopware-theme-config",
    }
)

# Mutating with no dryRun to hide behind.
UNSAFE = frozenset(
    {
        "merchant-cart-manage",  # creates and mutates carts
        "shopware-media-upload",  # creates a media entity from an upload
        "swag-dev-tools-scaffold",  # writes plugin scaffolding to disk
    }
)

READ_ONLY = frozenset(
    {
        "merchant-bestseller-report",
        "merchant-checkout-methods",
        "merchant-customer-lookup",
        "merchant-order-summary",
        "merchant-revenue-report",
        "merchant-storefront-search",
        "shopware-entity-aggregate",
        "shopware-entity-read",
        "shopware-entity-schema",
        "shopware-entity-search",
        "shopware-system-config-read",
        "shopware-tool-search",
        "shopware-toolset-enable",
        "shopware-toolsets-list",
        "swag-dev-tools-list-extensions",
        "swag-dev-tools-list-skills",
        "swag-dev-tools-load-skill",
        "swag-dev-tools-log-search",
        "swag-dev-tools-log-stream",
        "swag-dev-tools-notifications",
        # Shopware core despite riding the Store endpoint (see the
        # `shopware-store-api-` prefix in ownership.py), so it is classified
        # here rather than in ucp.py and survives that plugin's removal. Takes
        # no parameters at all, so there is nothing to mutate with.
        "shopware-store-api-context",
    }
)

# Merged rather than inlined: see the note above about dropping the plugin.
READ_ONLY |= ucp.READ_ONLY
DRY_RUNNABLE |= ucp.DRY_RUNNABLE
UNSAFE |= ucp.UNSAFE

DRY_RUN_KEY = "dryRun"


def classify(name: str) -> str | None:
    """READ_ONLY / DRY_RUNNABLE / UNSAFE as a string, or None if unclassified."""
    if name in READ_ONLY:
        return "read_only"
    if name in DRY_RUNNABLE:
        return "dry_runnable"
    if name in UNSAFE:
        return "unsafe"
    return None


def is_executable(name: str) -> bool:
    """Whether the eval may call this tool at all.

    Unclassified is not executable. A tool the server grew since the last
    snapshot has unknown blast radius, and defaulting to "probably fine" is how
    an eval ends up deleting something.
    """
    return classify(name) in ("read_only", "dry_runnable")


def prepare_call(name: str, args: dict) -> tuple[dict, bool]:
    """Arguments to send, and whether dryRun had to be forced on.

    A model that passes `dryRun: false` is overridden rather than obeyed. It is
    not being malicious — a fixture asking it to delete something makes that the
    literal-minded reading — but the eval's safety cannot depend on the thing
    under test agreeing to be safe.
    """
    if classify(name) != "dry_runnable":
        return dict(args or {}), False
    prepared = dict(args or {})
    forced = prepared.get(DRY_RUN_KEY) is not True
    prepared[DRY_RUN_KEY] = True
    return prepared, forced


def all_classified() -> frozenset[str]:
    return READ_ONLY | DRY_RUNNABLE | UNSAFE
