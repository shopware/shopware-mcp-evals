"""Real ids read off the instance under test.

The reason this module exists is a bug both suites had independently: a tool
handed an id that does not resolve fails, and the failure is charged to whoever
made the call rather than to the fixture that invented the id. So these tests
care most about the two places where a plausible-looking reply means nothing —
`add` answering `success: true` for a product the channel cannot sell, and the
several shapes a tool's `data` arrives in.
"""

import json
from typing import cast

import pytest

import lane
from eval.result_schema import JsonObject, McpResponse
from mcp_client import ADMIN, Endpoint
from tests.stubs import const


class Shop:
    """A shop with one sellable product, driven through the MCP call shape."""

    def __init__(
        self,
        sellable: tuple[str, ...] = ("p2",),
        token: str = "tok-1",
        rows: object = None,
    ) -> None:
        self.sellable: set[str] = set(sellable)
        self.token: str = token
        self.rows: object = rows
        self.items: list[JsonObject] = []
        self.added: list[str] = []
        self.calls: list[tuple[str, JsonObject]] = []

    def call(self, _session: str, tool: str, args: JsonObject, endpoint: Endpoint | None = None) -> JsonObject:
        assert endpoint is None or endpoint is ADMIN
        self.calls.append((tool, args))
        if tool == "shopware-entity-search":
            return {"data": self.rows if self.rows is not None else [{"id": "e-1"}]}
        if tool == "merchant-storefront-search":
            return {"data": {"products": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]}}
        action = args.get("action")
        if action == "create":
            return {"data": {"token": self.token}}
        if action == "add":
            product_id = str(args["productId"])
            self.added.append(product_id)
            if product_id in self.sellable:
                self.items.append({"id": "li-1", "referencedId": product_id})
            return {"data": {}}
        if action == "get":
            return {"data": {"lineItems": list(self.items)}}
        return {"data": {}}


@pytest.fixture
def shop(monkeypatch: pytest.MonkeyPatch) -> Shop:
    server = Shop()

    def call(session: str, tool: str, args: JsonObject, endpoint: Endpoint | None = None) -> McpResponse:
        assert endpoint is None or endpoint is ADMIN
        return cast(McpResponse, cast(object, {"_p": server.call(session, tool, args)}))

    def result_text(resp: McpResponse) -> str:
        payload = cast(JsonObject, cast(JsonObject, cast(object, resp))["_p"])
        return json.dumps({"success": True, **payload})

    monkeypatch.setattr(lane, "mcp_call", call)
    monkeypatch.setattr(lane, "mcp_result_text", result_text)
    return server


# ---------------------------------------------------------------------------
# Parsing: `data` has arrived as a bare list, {"elements": [...]} and a named
# collection. Every caller re-deriving that is how they drifted apart.
# ---------------------------------------------------------------------------
def test_payload_is_empty_rather_than_raising_on_a_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lane, "mcp_result_text", const("<html>gateway timeout</html>"))

    assert lane.payload(cast(McpResponse, cast(object, {}))) == {}


def test_payload_is_empty_when_the_body_is_not_an_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lane, "mcp_result_text", const("[1, 2]"))

    assert lane.payload(cast(McpResponse, cast(object, {}))) == {}


@pytest.mark.parametrize(
    "body,key,expected",
    [
        ({"data": [{"id": "a"}]}, "", [{"id": "a"}]),
        ({"data": {"elements": [{"id": "b"}]}}, "", [{"id": "b"}]),
        ({"data": {"products": [{"id": "c"}]}}, "products", [{"id": "c"}]),
        ({"data": {"lineItems": []}}, "lineItems", []),
        ({"data": None}, "", []),
        ({}, "", []),
    ],
)
def test_data_rows_tolerates_every_shape_the_server_has_used(
    monkeypatch: pytest.MonkeyPatch, body: JsonObject, key: str, expected: list[object]
) -> None:
    monkeypatch.setattr(lane, "mcp_result_text", const(json.dumps(body)))

    assert lane.data_rows(cast(McpResponse, cast(object, {})), key) == expected


# ---------------------------------------------------------------------------
# Looking ids up
# ---------------------------------------------------------------------------
def test_first_entity_id_asks_core_entity_search_so_it_works_without_plugins(shop: Shop) -> None:
    """The fixtures needing a product or customer id are core fixtures, and they
    have to resolve on an instance with no plugins installed at all."""
    assert lane.first_entity_id("sid", ADMIN, "product") == "e-1"
    assert shop.calls == [("shopware-entity-search", {"entity": "product", "limit": 1})]


def test_first_entity_id_is_empty_on_a_shop_with_no_such_rows(shop: Shop) -> None:
    shop.rows = []

    assert lane.first_entity_id("sid", ADMIN, "customer") == ""


def test_no_sales_channel_means_no_candidates_and_no_calls(shop: Shop) -> None:
    assert lane.sellable_products("sid", ADMIN, "") == []
    assert shop.calls == []


# ---------------------------------------------------------------------------
# Seeding a cart
# ---------------------------------------------------------------------------
def test_the_cart_is_read_back_because_add_reports_success_either_way(shop: Shop) -> None:
    """The whole point. `add` answers `success: true` for a product the channel
    cannot sell and puts nothing in the cart, so every call in the chain is
    green and checkout still fails with "Cart is empty"."""
    token, line_item_id = lane.create_cart("sid", ADMIN, "sc1", lane.sellable_products("sid", ADMIN, "sc1"))

    assert (token, line_item_id) == ("tok-1", "li-1")
    assert shop.added == ["p1", "p2"], "it stopped as soon as the cart read back non-empty"


def test_a_shop_with_nothing_sellable_yields_no_cart_rather_than_an_empty_one(shop: Shop) -> None:
    """("", "") is a claim about the lane, which callers turn into a SKIP naming
    the missing precondition. An empty cart's token would instead look usable
    and fail downstream as if checkout were broken."""
    shop.sellable = set()

    assert lane.create_cart("sid", ADMIN, "sc1", ["p1", "p2", "p3"]) == ("", "")
    assert shop.added == ["p1", "p2", "p3"], "every candidate was tried before giving up"


def test_no_sales_channel_means_no_cart(shop: Shop) -> None:
    assert lane.create_cart("sid", ADMIN, "", ["p1"]) == ("", "")
    assert shop.calls == []


def test_a_shop_that_will_not_open_a_cart_yields_nothing(shop: Shop) -> None:
    shop.token = ""

    assert lane.create_cart("sid", ADMIN, "sc1", ["p2"]) == ("", "")
    assert shop.added == [], "nothing was added to a cart that does not exist"
