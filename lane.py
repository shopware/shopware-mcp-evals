#!/usr/bin/env python3
"""Real ids, read off the instance under test.

Both suites need the same thing and needed it for the same reason: grading now
EXECUTES the call. A tool that is handed an id which does not resolve fails, and
that failure is charged to whoever made the call — the model in eval/, the tool
in functional/. Neither is the truth. The truth is that the id was invented.

The demo data is generated (`framework:demodata`), so there is no fixed product,
customer or order to hardcode: an id that works on one lane is a 500 on the
next. Everything here is therefore a lookup, resolved once at startup and
substituted before anything is graded.

Kept out of both runners because they had begun to disagree. functional/ had
learned that "the storefront returned this product" does not mean the channel
can sell it; eval/ had not, and its three cart fixtures failed on every run with
the model having picked exactly the right tool. One definition of "a cart that
can be checked out" is the point of this module.

Read-only by default. `create_cart` is the exception and is marked as such at
every call site — it writes to the shop, which is only ever acceptable on an
instance that is about to be destroyed.
"""

import json
from typing import cast

from eval.result_schema import JsonObject, McpResponse, as_object
from mcp_client import Endpoint, mcp_call, mcp_result_text


def payload(resp: McpResponse) -> JsonObject:
    """The JSON object carried in a tools/call text content block, or {}."""
    try:
        parsed = cast(object, json.loads(mcp_result_text(resp) or "{}"))
    except (ValueError, TypeError):
        return {}
    return cast(JsonObject, parsed) if isinstance(parsed, dict) else {}


def data_rows(resp: McpResponse, key: str = "") -> list[object]:
    """The list of records in a tool reply, whichever shape it arrived in.

    `data` has been a bare list, `{"elements": [...]}` and — for the merchant
    tools — a named collection like `{"products": [...]}`. Tolerating all three
    here keeps every caller from re-deriving it and getting it subtly wrong.
    """
    data = payload(resp).get("data")
    if isinstance(data, dict):
        inner = cast(JsonObject, data)
        data = inner.get(key) if key else (inner.get("elements") or inner.get("data"))
    return cast(list[object], data) if isinstance(data, list) else []


def first_entity_id(session: str, endpoint: Endpoint, entity: str) -> str:
    """The id of any one row of `entity`, via the core entity-search tool.

    Core, not merchant-*: the fixtures that need a product or customer id are
    core entity fixtures, and they have to resolve on an instance with no
    plugins installed at all.
    """
    resp = mcp_call(session, "shopware-entity-search", {"entity": entity, "limit": 1}, endpoint=endpoint)
    rows = data_rows(resp)
    return str(as_object(rows[0]).get("id", "")) if rows else ""


def sellable_products(session: str, endpoint: Endpoint, sales_channel_id: str) -> list[str]:
    """Products the storefront might be able to sell in this sales channel.

    Not the same as "a product row exists". entity-search happily returns a
    product that is inactive, out of stock, or not assigned to this channel, and
    adding one of those to a cart answers `success: true` with an empty
    lineItems array — a silent no-op that made the checkout check fail with
    "Cart is empty" while looking like a checkout bug.

    A LIST, not the first hit, because the storefront search is not that filter
    either: it ranks by relevance, and its top result was still landing in an
    empty cart on CI. The only reliable test of "sellable" is adding it and
    reading the cart back, which is what create_cart does — so this owes it
    candidates to try rather than one guess.
    """
    if not sales_channel_id:
        return []
    resp = mcp_call(
        session,
        "merchant-storefront-search",
        {"salesChannelId": sales_channel_id, "term": "a", "limit": 10},
        endpoint=endpoint,
    )
    return [str(pid) for row in data_rows(resp, "products") if (pid := as_object(row).get("id"))]


def cart_line_items(session: str, endpoint: Endpoint, sales_channel_id: str, token: str) -> list[object]:
    """The cart's line items, read back off the server.

    The `add` call answering `success: true` does not mean anything went in —
    for a product the channel cannot sell it is a no-op with a 200. Reading the
    cart is the only statement about its contents that holds.
    """
    resp = mcp_call(
        session,
        "merchant-cart-manage",
        {"salesChannelId": sales_channel_id, "action": "get", "token": token},
        endpoint=endpoint,
    )
    return data_rows(resp, "lineItems")


def create_cart(session: str, endpoint: Endpoint, sales_channel_id: str, product_ids: list[str]) -> tuple[str, str]:
    """MUTATES: open a cart, put something in it, return (token, line_item_id).

    The line item is the point. An empty cart is rejected with "Cart is empty.
    Add items with merchant-cart-manage first", so anything downstream was
    asserting that an empty cart cannot be ordered — which nobody doubted.

    Each candidate is added and then the cart is READ BACK, because that is the
    only thing that distinguishes a product the channel can sell from one it
    silently declines. Adding the first search hit and trusting `success: true`
    is what left CI failing on "Cart is empty" with every prior call green.

    Returns ("", "") when no candidate lands in the cart. Callers treat that as
    missing data about the lane — a SKIP naming the absent precondition — and
    not as evidence that checkout is broken.
    """
    if not sales_channel_id:
        return "", ""
    created = mcp_call(
        session,
        "merchant-cart-manage",
        {"salesChannelId": sales_channel_id, "action": "create"},
        endpoint=endpoint,
    )
    token = str(as_object(payload(created).get("data")).get("token", ""))
    if not token:
        return "", ""
    for product_id in product_ids:
        mcp_call(
            session,
            "merchant-cart-manage",
            {
                "salesChannelId": sales_channel_id,
                "action": "add",
                "token": token,
                "productId": product_id,
                "quantity": 1,
            },
            endpoint=endpoint,
        )
        items = cart_line_items(session, endpoint, sales_channel_id, token)
        if items:
            return token, str(as_object(items[0]).get("id", ""))
    return "", ""
