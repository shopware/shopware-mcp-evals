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
    detail: str | Callable[[dict], str] = ""
    args: Callable[[dict], dict] = field(default=lambda _ctx: {})
    requires: tuple[tuple[str, str], ...] = ()

    def label(self, ctx: dict) -> str:
        detail = self.detail(ctx) if callable(self.detail) else self.detail
        return f"{self.tool} {detail}".strip()

    def skip_label(self, reason: str) -> str:
        return f"{self.tool} ({reason})"

    def blocked_by(self, ctx: dict) -> str | None:
        """The reason this check cannot run, or None. First missing key wins."""
        for key, reason in self.requires:
            if not ctx.get(key):
                return reason
        return None


# A real, fetchable image. The previous value (assets.shopware.com) answers 403,
# so every run reported the tool broken when the fixture was. Override for an
# instance with no outbound network.
MEDIA_UPLOAD_URL = os.environ.get(
    "MCP_MEDIA_UPLOAD_URL",
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
)


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
            "url": MEDIA_UPLOAD_URL,
            "fileName": f"mcp-test-{uuid.uuid4().hex[:12]}.png",
        },
        # Set from --skip-media-upload: the only check that writes a real file.
        (("media_upload_enabled", "--skip-media-upload"),),
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
        (
            ("sales_channel_id", "no storefront sales channel"),
            ("cart_token", "could not get cart token or customer ID"),
            ("customer_id", "could not get cart token or customer ID"),
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
    # `file` defaults to "", which is never a real filename — both readers
    # failed with "Log file not found" on every instance, naming the valid
    # values in the error. gather_context asks for that list rather than
    # guessing a name that depends on the date and APP_ENV.
    ToolCheck(
        "swag-dev-tools-log-search",
        "(query: error)",
        lambda c: {"query": "error", "limit": 5, "file": c["log_file"]},
        (("log_file", "no log files on this instance"),),
    ),
    ToolCheck(
        "swag-dev-tools-log-stream",
        "(last 10)",
        lambda c: {"limit": 10, "file": c["log_file"]},
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
