#!/usr/bin/env python3
"""The per-tool assertion table for the admin endpoint.

This was 261 lines of straight-line code in the runner: 27 `assert_tool(...)`
calls, 14 `rep.skip(...)` calls, and the same six-argument call signature
repeated at every one. Adding a tool meant adding a paragraph; running a single
tool meant editing the file.

Each entry is now data. `args` is a callable because most payloads need an id
fetched from the live server first (a product to read, a sales channel to price
against) — the callable receives that context rather than the table being
rebuilt per run.

`requires` is an ordered tuple of (context key, reason). The first key that is
missing or empty decides the skip, and its reason goes in the label, so
`merchant-cart-checkout` can report "no storefront sales channel" or "could not
get cart token or customer ID" depending on which prerequisite actually failed.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from eval.result_schema import JsonObject
from mcp_client import SW_BASE_URL

# The live ids and flags gather_context assembles, keyed by the names `requires`
# refers to. Values are heterogeneous (ids are strings, --skip flags are bools),
# so it is the same string-keyed map every other JSON shape here uses.
type Context = JsonObject

# A phantom UUID that cannot exist — used for dryRun delete assertions.
# 32 hex characters with NO dashes. That is the only form Shopware's DAL accepts:
# the dashed form is rejected with "Value is not a valid UUID", so the delete
# check was asserting the argument validator rather than the tool it names.
#
# Defined here and imported by the runner. It used to be declared in both, with
# different values, and the table's copy — the one that decides the payload — was
# the wrong one.
ZERO_UUID = "0" * 32


@dataclass(frozen=True)
class ToolCheck:
    tool: str
    # Suffix for the pass/fail label, e.g. "(product by ID)". The tool name is
    # prepended, so it never has to be repeated here. A callable when the detail
    # is only known at run time — load-skill names the skill it actually loaded,
    # which is the difference between "it worked" and "it worked on this input".
    detail: str | Callable[[Context], str] = ""
    args: Callable[[Context], JsonObject] = field(default=lambda _ctx: {})
    requires: tuple[tuple[str, str], ...] = ()
    # Text the response must contain. Without it a check only asserts that the
    # tool answered *something*, which for a reader is satisfied by an empty
    # result — the tool can be pointed at the wrong file, find nothing, and pass.
    # With it, the check proves the tool returned the thing we know is there.
    contains: str = ""

    def label(self, ctx: Context) -> str:
        detail = self.detail(ctx) if callable(self.detail) else self.detail
        return f"{self.tool} {detail}".strip()

    def skip_label(self, reason: str) -> str:
        return f"{self.tool} ({reason})"

    def blocked_by(self, ctx: Context) -> str | None:
        """The reason this check cannot run, or None. First missing key wins."""
        for key, reason in self.requires:
            if not ctx.get(key):
                return reason
        return None


# The image shopware-media-upload fetches. Served by the shop itself, from a file
# committed at functional/assets/ — see its README.
#
# Both previous values were URLs on somebody else's host, and both broke in a way
# that read as a tool bug: assets.shopware.com answered 403, then
# upload.wikimedia.org 404'd once the file was removed and failed the whole static
# job with 47 of 48 checks passing. Neither said anything about the tool.
#
# A local run has to put the file where its shop serves it (the README has the one
# command) or point MCP_MEDIA_UPLOAD_URL elsewhere. Without either, the check
# SKIPs rather than failing — see `media_upload_url` below.
MEDIA_UPLOAD_URL = os.environ.get("MCP_MEDIA_UPLOAD_URL", f"{SW_BASE_URL}/mcp-evals-probe.png")

# The line the lane writes into the server's log during setup, and the thing the
# log readers are then asked to find. Static on both sides on purpose: a check
# that searches for whatever happens to be in the file cannot tell "the reader
# works" from "the file had something in it".
#
# Distinctive enough that it cannot match anything Shopware itself logs, so a
# hit is proof the reader opened the file we seeded rather than a coincidence.
LOG_PROBE_TEXT = "mcp-evals lane probe 4f21a7"


CORE_CHECKS: tuple[ToolCheck, ...] = (
    ToolCheck("shopware-entity-schema", "(product)", lambda c: {"entity": "product"}),
    ToolCheck("shopware-entity-search", "(product, limit 1)", lambda c: {"entity": "product", "limit": 1}),
    ToolCheck(
        "shopware-entity-read",
        "(product by ID)",
        lambda c: {"entity": "product", "id": c["product_id"]},
        (("product_id", "no product found"),),
    ),
    ToolCheck(
        "shopware-entity-aggregate",
        "(count products)",
        lambda c: {
            "entity": "product",
            "aggregations": json.dumps([{"name": "total", "type": "count", "field": "id"}]),
        },
    ),
    ToolCheck(
        "shopware-entity-upsert",
        "(dryRun)",
        lambda c: {
            "entity": "product",
            "payload": json.dumps({"id": c["product_id"], "stock": 1}),
            "dryRun": True,
        },
        (("product_id", "no product found"),),
    ),
    ToolCheck(
        "shopware-entity-delete",
        "(dryRun)",
        lambda c: {"entity": "product", "ids": json.dumps([ZERO_UUID]), "dryRun": True},
    ),
    ToolCheck("shopware-system-config-read", "", lambda c: {"key": "core.basicInformation"}),
    ToolCheck(
        "shopware-system-config-write",
        "(dryRun)",
        lambda c: {"key": "core.basicInformation.shopName", "value": json.dumps("Test"), "dryRun": True},
    ),
    ToolCheck(
        "shopware-order-state",
        "(dryRun)",
        # At least one action is required — the tool says so, and without it the
        # call never reaches the state machine this check exists to exercise.
        # `process` is a transition every order in any state can be previewed for.
        lambda c: {"orderId": c["order_id"], "orderAction": "process", "dryRun": True},
        (("order_id", "no order found"),),
    ),
    ToolCheck(
        "shopware-media-upload",
        "",
        # Two things were wrong here, and both read as the tool failing. The URL
        # answers 403, so the fetch could never succeed; and the extension comes
        # from `fileName`, not from the URL, so an extensionless name fails with
        # 'The file extension "" ... is not supported' even for a URL that works.
        # The name has to be unique per run. A fixed one uploads fine once and
        # then fails with 'A file with the name "…" already exists' on every
        # later run against the same instance — green in CI, where the instance
        # is new each time, and broken on the trunk lane anyone tests against.
        lambda c: {
            "url": c["media_upload_url"],
            "fileName": f"mcp-test-{uuid.uuid4().hex[:12]}.png",
        },
        # `media_upload_enabled` is set from --skip-media-upload: this is the only
        # check that writes a real file. `media_upload_url` is the URL only if
        # something is actually served there, so an unseeded lane SKIPs with the
        # reason instead of reporting the tool broken over a 404 it could not have
        # avoided.
        (
            ("media_upload_enabled", "--skip-media-upload"),
            ("media_upload_url", f"no image served at {MEDIA_UPLOAD_URL}; see functional/assets/README.md"),
        ),
    ),
    ToolCheck(
        "shopware-theme-config",
        "(get)",
        lambda c: {"salesChannelId": c["sales_channel_id"], "action": "get"},
        (("sales_channel_id", "no storefront sales channel found"),),
    ),
)

MERCHANT_CHECKS: tuple[ToolCheck, ...] = (
    ToolCheck(
        "merchant-customer-lookup",
        "(by email)",
        lambda c: {"email": c["customer_email"]},
        (("customer_email", "no customer found"),),
    ),
    ToolCheck(
        "merchant-order-summary",
        "(by ID)",
        lambda c: {"orderId": c["order_id"]},
        (("order_id", "no order found"),),
    ),
    ToolCheck(
        "merchant-checkout-methods",
        "",
        lambda c: {"salesChannelId": c["sales_channel_id"]},
        (("sales_channel_id", "no storefront sales channel"),),
    ),
    ToolCheck(
        "merchant-storefront-search",
        "(term: shirt)",
        lambda c: {"salesChannelId": c["sales_channel_id"], "term": "shirt"},
        (("sales_channel_id", "no storefront sales channel"),),
    ),
    ToolCheck(
        "merchant-cart-manage",
        "(create)",
        lambda c: {"salesChannelId": c["sales_channel_id"], "action": "create"},
        (("sales_channel_id", "no storefront sales channel"),),
    ),
    ToolCheck(
        "merchant-cart-checkout",
        "(dryRun)",
        lambda c: {
            "salesChannelId": c["sales_channel_id"],
            "token": c["cart_token"],
            "customerId": c["customer_id"],
            "dryRun": True,
        },
        # Order matters: no sales channel is a different finding from a cart that
        # could not be created inside one that exists.
        #
        # `cart_token` is empty only when no storefront-visible product could be
        # added to a cart at all (see create_cart_token), which is missing data
        # about the lane, not evidence about checkout. Saying so is the whole
        # value of the distinction — the alternative is a FAIL reading "Cart is
        # empty" that sends someone to read the checkout tool.
        (
            ("sales_channel_id", "no storefront sales channel"),
            ("cart_token", "no sellable product in this channel, so no cart to check out"),
            ("customer_id", "no customer found"),
        ),
    ),
    ToolCheck(
        "merchant-product-create",
        "(dryRun)",
        lambda c: {
            "name": "MCP Test Product",
            "productNumber": "MCP-TEST-001",
            "grossPrice": 9.99,
            "dryRun": True,
        },
    ),
    ToolCheck(
        "merchant-bestseller-report",
        "",
        lambda c: {"from": "2025-01-01", "to": "2025-12-31", "limit": 5},
    ),
    ToolCheck(
        "merchant-revenue-report",
        "(groupBy month)",
        lambda c: {"from": "2025-01-01", "to": "2025-12-31", "groupBy": "month"},
    ),
)

DEV_CHECKS: tuple[ToolCheck, ...] = (
    # A known line in a known file, so these assert that the reader *found what
    # we put there* rather than that it answered. "The tool returned something"
    # is satisfied by an empty result — a reader pointed at the wrong file finds
    # nothing and passes, which is how both of these looked healthy while
    # failing.
    #
    # The lane writes LOG_PROBE_TEXT during setup; gather_context finds the file
    # containing it. Where no lane seeded one — someone else's shop — the checks
    # fall back to the newest real log and assert only that the call worked,
    # because there is nothing known to look for.
    ToolCheck(
        "swag-dev-tools-log-search",
        lambda c: f"(finds the seeded line in {c['log_file']})" if c.get("log_probe") else "(query: error)",
        lambda c: {"query": LOG_PROBE_TEXT if c.get("log_probe") else "error", "limit": 5, "file": c["log_file"]},
        (("log_file", "no log files on this instance"),),
        contains=LOG_PROBE_TEXT,
    ),
    ToolCheck(
        "swag-dev-tools-log-stream",
        "(last 10)",
        lambda c: {"limit": 50, "file": c["log_file"]},
        (("log_file", "no log files on this instance"),),
    ),
    ToolCheck("swag-dev-tools-list-extensions", ""),
    ToolCheck("swag-dev-tools-list-skills", ""),
    # scaffold with no args lists the available types — non-destructive.
    ToolCheck("swag-dev-tools-scaffold", "(list types)"),
    # notifications: wait=false so it never opens an SSE stream in tests.
    ToolCheck("swag-dev-tools-notifications", "(poll)", lambda c: {"wait": False, "limit": 5}),
    # load-skill needs a real skill name, pulled from list-skills at run time.
    ToolCheck(
        "swag-dev-tools-load-skill",
        lambda c: f"({c['skill_name']})",
        lambda c: {"name": c["skill_name"]},
        (("skill_name", "no skills found"),),
    ),
)

ALL_CHECKS: tuple[ToolCheck, ...] = CORE_CHECKS + MERCHANT_CHECKS + DEV_CHECKS
