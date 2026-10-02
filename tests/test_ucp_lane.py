"""The Store eval's lane lookups: real ids off the UCP tools, offline.

Each test hands `ucp` a fake ToolCall that answers the way the plugin does —
`{"success": true, "data": {...}}`, and a refusal in-band as `success: false` —
so what is under test is how the answers are read, not the server.
"""

from collections.abc import Iterator
from typing import cast

import pytest

import ucp
from eval import runner as E
from eval.result_schema import Fixture, JsonObject, McpResponse, as_list, as_object
from mcp_client import Endpoint, store_endpoint
from tests.stubs import const

UNPURCHASABLE = "p-not-in-channel"
EMPTY_RESPONSE: McpResponse = {}


def ok(data: JsonObject) -> JsonObject:
    return {"success": True, "data": data}


REFUSED: JsonObject = {"success": False, "error": {"code": "validation", "message": "could not be added"}}


class FakeUcp:
    """Answers like the plugin and records every call it was given."""

    def __init__(self, products: list[str]) -> None:
        self.products: list[str] = products
        self.calls: list[tuple[str, JsonObject]] = []

    def __call__(self, tool: str, arguments: JsonObject) -> JsonObject:
        self.calls.append((tool, arguments))
        if tool == "search_catalog":
            return ok({"products": [{"id": p} for p in self.products]})
        line = as_object(as_list(as_object(arguments.get("payload")).get("line_items"))[0])
        product = str(as_object(line.get("item")).get("id"))
        if product == UNPURCHASABLE:
            return REFUSED
        if tool == "create_cart":
            return ok({"id": "cart-1", "line_items": [{"id": f"li_{product}"}]})
        if tool == "create_checkout":
            return ok({"id": "co-1", "status": "incomplete"})
        raise AssertionError(f"unexpected tool {tool}")

    def committed(self) -> list[str]:
        return [tool for tool, args in self.calls if args.get("dryRun") is False]


def test_the_first_product_a_cart_accepts_is_chosen_not_the_first_one_found() -> None:
    """The bug this closes: the fixtures named a searchable product a cart
    refused, and a correct create_cart was graded invalid_arguments."""
    fake = FakeUcp([UNPURCHASABLE, "p-ok"])

    assert ucp.purchasable_product_id(fake) == "p-ok"
    assert fake.committed() == [], "finding the product must not write to the shop"


def test_no_purchasable_product_resolves_to_nothing() -> None:
    assert ucp.purchasable_product_id(FakeUcp([UNPURCHASABLE])) == ""
    assert ucp.purchasable_product_id(FakeUcp([])) == ""


def test_a_refused_search_resolves_to_nothing() -> None:
    assert ucp.purchasable_product_id(const(REFUSED)) == ""


def test_seeding_creates_one_cart_and_one_checkout_and_names_the_carts_own_line() -> None:
    fake = FakeUcp(["p-ok"])

    assert ucp.seed_lane(fake) == {"cart_id": "cart-1", "line_item_id": "li_p-ok", "checkout_id": "co-1"}
    assert fake.committed() == ["create_cart", "create_checkout"]


def test_seeding_without_a_purchasable_product_creates_nothing() -> None:
    fake = FakeUcp([UNPURCHASABLE])

    assert ucp.seed_lane(fake) == {}
    assert fake.committed() == []


def test_a_refused_creation_leaves_its_ids_empty_so_their_fixtures_skip() -> None:
    def refuse_checkout(tool: str, arguments: JsonObject) -> JsonObject:
        return REFUSED if tool == "create_checkout" else FakeUcp(["p-ok"])(tool, arguments)

    assert ucp.seed_lane(refuse_checkout) == {"cart_id": "cart-1", "line_item_id": "li_p-ok", "checkout_id": ""}


# ---------------------------------------------------------------------------
# runner wiring
# ---------------------------------------------------------------------------
STORE: Endpoint = store_endpoint(access_key="k", context_token="t")


def fx(fid: str, prompt: str) -> Fixture:
    base: JsonObject = {"id": fid, "prompt": prompt}
    return cast(Fixture, cast(object, base))


def replies(*texts: str) -> object:
    """mcp_result_text, answering each call with the next of `texts`."""
    it: Iterator[str] = iter(texts)

    def stub(_resp: McpResponse) -> str:
        return next(it)

    return stub


def test_the_store_endpoint_resolves_through_ucp_not_the_admin_tools() -> None:
    """The admin resolvers call entity-search, which the Store endpoint does not
    have, so on store they would fail and skip every fixture that used them."""
    assert E._resolvers_for(STORE) == (E.STORE_PLACEHOLDER_RESOLVERS, E.STORE_SEEDING_RESOLVERS)
    assert E._resolvers_for(E.ADMIN) == (E.PLACEHOLDER_RESOLVERS, E.SEEDING_RESOLVERS)


def test_store_placeholders_are_known_so_an_unresolved_one_skips_its_fixture() -> None:
    assert {"product_id", "cart_id", "line_item_id", "checkout_id"} <= E.KNOWN_PLACEHOLDERS


def test_store_seeding_waits_for_seed_lane(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(E.STORE_PLACEHOLDER_RESOLVERS, "product_id", const("p-ok"))
    fixtures = [fx("a", "cart {cart_id}"), fx("b", "product {product_id}")]

    subs = E.resolve_lane_substitutions(fixtures, endpoint=STORE, seed_lane=False)

    assert subs == {"product_id": "p-ok"}
    assert "Lane seeding off (--seed-lane): cart_id" in capsys.readouterr().out


def test_the_runner_reads_the_ucp_answer_off_one_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[Endpoint] = []

    def init(endpoint: Endpoint | None = None) -> tuple[str, str]:
        assert endpoint is not None
        sessions.append(endpoint)
        return "sid", ""

    monkeypatch.setattr(E, "mcp_init", init)
    monkeypatch.setattr(E, "mcp_call", const(EMPTY_RESPONSE))
    monkeypatch.setattr(
        E,
        "mcp_result_text",
        replies('{"success": true, "data": {"products": [{"id": "p-ok"}]}}', '{"success": true, "data": {"id": "c"}}'),
    )

    assert E._ucp_product_id(STORE) == "p-ok"
    assert sessions == [STORE]


def test_an_unreadable_ucp_answer_reads_as_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E, "mcp_init", const(("sid", "")))
    monkeypatch.setattr(E, "mcp_call", const(EMPTY_RESPONSE))
    monkeypatch.setattr(E, "mcp_result_text", const("not json"))

    assert E._ucp_product_id(STORE) is None
