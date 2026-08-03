"""Real ids read off the instance under test.

The reason this module exists is a bug both suites had independently: a tool
handed an id that does not resolve fails, and the failure is charged to whoever
made the call rather than to the fixture that invented the id. So these tests
care most about the two places where a plausible-looking reply means nothing —
`add` answering `success: true` for a product the channel cannot sell, and the
several shapes a tool's `data` arrives in.
"""

import json

import pytest

import lane


class Shop:
    """A shop with one sellable product, driven through the MCP call shape."""

    def __init__(self, sellable=("p2",), token="tok-1", rows=None):
        self.sellable = set(sellable)
        self.token = token
        self.rows = rows
        self.items: list[dict] = []
        self.added: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    def call(self, _session, tool, args, endpoint=None):
        self.calls.append((tool, args))
        if tool == "shopware-entity-search":
            return {"data": self.rows if self.rows is not None else [{"id": "e-1"}]}
        if tool == "merchant-storefront-search":
            return {"data": {"products": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]}}
        action = args.get("action")
        if action == "create":
            return {"data": {"token": self.token}}
        if action == "add":
            self.added.append(args["productId"])
            if args["productId"] in self.sellable:
                self.items.append({"id": "li-1", "referencedId": args["productId"]})
            return {"data": {}}
        if action == "get":
            return {"data": {"lineItems": list(self.items)}}
        return {"data": {}}


@pytest.fixture
def shop(monkeypatch):
    server = Shop()
    monkeypatch.setattr(lane, "mcp_call", lambda s, t, a, endpoint=None: {"_p": server.call(s, t, a)})
    monkeypatch.setattr(lane, "mcp_result_text", lambda r: json.dumps({"success": True, **r["_p"]}))
    return server


# ---------------------------------------------------------------------------
# Parsing: `data` has arrived as a bare list, {"elements": [...]} and a named
# collection. Every caller re-deriving that is how they drifted apart.
# ---------------------------------------------------------------------------
def test_payload_is_empty_rather_than_raising_on_a_non_json_body(monkeypatch):
    monkeypatch.setattr(lane, "mcp_result_text", lambda _r: "<html>gateway timeout</html>")

    assert lane.payload({}) == {}


def test_payload_is_empty_when_the_body_is_not_an_object(monkeypatch):
    monkeypatch.setattr(lane, "mcp_result_text", lambda _r: "[1, 2]")

    assert lane.payload({}) == {}


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
def test_data_rows_tolerates_every_shape_the_server_has_used(monkeypatch, body, key, expected):
    monkeypatch.setattr(lane, "mcp_result_text", lambda _r: json.dumps(body))

    assert lane.data_rows({}, key) == expected


# ---------------------------------------------------------------------------
# Looking ids up
# ---------------------------------------------------------------------------
def test_first_entity_id_asks_core_entity_search_so_it_works_without_plugins(shop):
    """The fixtures needing a product or customer id are core fixtures, and they
    have to resolve on an instance with no plugins installed at all."""
    assert lane.first_entity_id("sid", None, "product") == "e-1"
    assert shop.calls == [("shopware-entity-search", {"entity": "product", "limit": 1})]


def test_first_entity_id_is_empty_on_a_shop_with_no_such_rows(shop):
    shop.rows = []

    assert lane.first_entity_id("sid", None, "customer") == ""


def test_no_sales_channel_means_no_candidates_and_no_calls(shop):
    assert lane.sellable_products("sid", None, "") == []
    assert shop.calls == []


# ---------------------------------------------------------------------------
# Seeding a cart
# ---------------------------------------------------------------------------
def test_the_cart_is_read_back_because_add_reports_success_either_way(shop):
    """The whole point. `add` answers `success: true` for a product the channel
    cannot sell and puts nothing in the cart, so every call in the chain is
    green and checkout still fails with "Cart is empty"."""
    token, line_item_id = lane.create_cart("sid", None, "sc1", lane.sellable_products("sid", None, "sc1"))

    assert (token, line_item_id) == ("tok-1", "li-1")
    assert shop.added == ["p1", "p2"], "it stopped as soon as the cart read back non-empty"


def test_a_shop_with_nothing_sellable_yields_no_cart_rather_than_an_empty_one(shop):
    """("", "") is a claim about the lane, which callers turn into a SKIP naming
    the missing precondition. An empty cart's token would instead look usable
    and fail downstream as if checkout were broken."""
    shop.sellable = set()

    assert lane.create_cart("sid", None, "sc1", ["p1", "p2", "p3"]) == ("", "")
    assert shop.added == ["p1", "p2", "p3"], "every candidate was tried before giving up"


def test_no_sales_channel_means_no_cart(shop):
    assert lane.create_cart("sid", None, "", ["p1"]) == ("", "")
    assert shop.calls == []


def test_a_shop_that_will_not_open_a_cart_yields_nothing(shop):
    shop.token = ""

    assert lane.create_cart("sid", None, "sc1", ["p2"]) == ("", "")
    assert shop.added == [], "nothing was added to a cart that does not exist"
