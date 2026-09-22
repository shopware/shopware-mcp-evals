"""The UCP buyer journey.

The journey exists because these tools are one flow: an isolated call to
`cart-get` with an invented id measures how the server words "not found", which
is not a fact about the tool. So the tests here care about the two properties
that make the flow meaningful — ids thread forward, and nothing writes without
consent — rather than re-asserting the step list.
"""

import json
from typing import cast

import pytest

from eval.result_schema import JsonObject, McpResponse, as_list, as_object
from functional import journeys
from functional.reporting import Reporter
from mcp_client import Endpoint, store_endpoint
from tests.stubs import const

# A real Endpoint pointing nowhere: every call below is stubbed, so only its
# identity and its url (which the reporter prints) matter.
STORE: Endpoint = store_endpoint()


def _reporter() -> Reporter:
    return Reporter("test")


def test_the_guard_blocks_every_step_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """Committing is the whole point, so an unguarded journey must write nothing
    at all — not "fewer writes", none. A call escaping here would be a real order
    on whatever shop the suite happens to point at."""
    called: list[tuple[object, ...]] = []

    def record(*args: object, **_kwargs: object) -> McpResponse:
        called.append(args)
        return {}

    monkeypatch.setattr(journeys, "mcp_call", record)

    rep = _reporter()
    ctx = journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=False)

    assert called == [], "the guard let a call through"
    assert ctx == {}
    assert rep.skipped == len(journeys.UCP_JOURNEY)
    assert rep.passed == 0 and rep.failed == 0
    assert all("--allow-mutations" in r.get("reason", "") for r in rep.records)


def test_a_broken_step_skips_its_dependants_naming_the_precondition() -> None:
    """One early break must not cascade into a wall of red that hides where it
    started. The reason names the missing key, which points at the step that
    should have produced it — "not found" would not."""
    step = next(s for s in journeys.UCP_JOURNEY if "cart_id" in s.needs)

    assert step.missing({}) in step.needs
    assert step.missing(dict.fromkeys(step.needs, "x")) == ""


def test_every_step_after_the_first_declares_its_preconditions() -> None:
    """A step with no `needs` runs unconditionally. Only the opening search is
    entitled to that; anything else would call with an id that is silently empty
    and be graded on the resulting not-found."""
    for step in journeys.UCP_JOURNEY[1:]:
        assert step.needs, f"{step.tool} declares no preconditions"


def test_ids_thread_forward_through_the_flow() -> None:
    """The property that distinguishes a journey from a table of calls: what one
    step produces is what the next consumes.

    `query` and `promo_code` are the only exceptions, and both are configuration
    rather than results — there is no way to discover a promotion code through
    the Store API, so it arrives from the environment or its step skips.
    """
    produced = {"query", "promo_code"}
    for step in journeys.UCP_JOURNEY:
        missing = set(step.needs) - produced
        assert not missing, f"{step.tool} needs {sorted(missing)}, which no earlier step produces"
        ctx: JsonObject = {}
        # `line_items` carries ids because a checkout response is REQUIRED to
        # carry them (generated checkout.get.response, branch
        # dev.ucp.shopping.checkout: required id, item, quantity, totals). A fake
        # without them is not a response the server can produce, and leaving it
        # out made this test claim nothing produces `line_item_ids`.
        step.capture(
            {
                "id": "x",
                "products": [{"id": "p", "title": "t"}],
                "line_items": [{"id": "li-1", "item": {"id": "p"}, "quantity": 1}],
                "order": {"id": "o"},
            },
            ctx,
        )
        produced |= {key for key, value in ctx.items() if value}


def test_line_items_use_the_nested_item_shape() -> None:
    """Measured against a live lane: a flat {"id": …} is rejected by the schema.
    Pinned because it is not derivable from the tool's declared parameters."""
    items = journeys._line_items({"product_id": "abc"})

    assert items == [{"item": {"id": "abc"}, "quantity": 1}]


def test_checkout_update_resends_the_whole_line_items_array() -> None:
    """checkout.update is PUT, not PATCH. An agent told to "add a shipping
    address" sends only the address and fails every time — so the journey has to
    demonstrate the working shape."""
    step = next(s for s in journeys.UCP_JOURNEY if s.tool == "update_checkout")
    ctx: JsonObject = {"checkout_id": "c", "product_id": "p", "line_item_ids": ["li-1"]}
    payload = as_object(step.args(ctx)["payload"])

    assert payload["line_items"], "update dropped line_items"
    assert "buyer" in payload
    assert "payment" in payload, "without payment the checkout never reaches ready_for_complete"
    # `fulfillment`, NOT a top-level `fulfillment_address` — that spelling is not
    # a property of checkout.create/update at all, so it was accepted and dropped,
    # and only surfaced at completion as "Checkout session is missing
    # fulfillment.shipping_address".
    assert "fulfillment_address" not in payload, "not a schema property; silently ignored"
    method = as_object(as_list(as_object(payload["fulfillment"])["methods"])[0])
    assert method["line_item_ids"] == ["li-1"], "the required field has to name real line items"
    # The destination, in the one shape the oneOf accepts, and this pins a
    # measurement rather than a preference. Branch 0 (shipping_destination)
    # REQUIRES `id`; branch 1 (retail_location) requires `id` and `name`. So an
    # `id` with the address inline and no `name` matches branch 0 alone, while a
    # bare address matches neither and anything carrying `name` matches both.
    # See journeys.DESTINATION_SHAPE for the measured table.
    destination = as_object(as_list(method["destinations"])[0])
    assert destination["id"], "branch 0 requires an id; without one the oneOf matches nothing"
    assert "name" not in destination, "name pulls the object into retail_location too, matching both branches"
    assert destination["street_address"], "schema.org names, not Shopware's street/zipcode/city"
    assert method["selected_destination_id"] == destination["id"]


def test_cart_update_does_not_repeat_the_id_inside_the_payload() -> None:
    """This test used to assert the opposite, and pinned a real quirk: the tool
    took `id` as a required parameter and then rejected the request for `$.id is
    required` anyway, so the journey sent it twice.

    UCP 2026-08-25 fixed it and the description now forbids what the fix allowed
    to stop being necessary — "the cart id travels as the id parameter and is not
    repeated in the payload". Inverted rather than deleted, because the duplicate
    is the shape someone reading the old journey would copy.
    """
    step = next(s for s in journeys.UCP_JOURNEY if s.tool == "update_cart")
    args = step.args({"cart_id": "cart-1", "product_id": "p"})

    assert args["id"] == "cart-1"
    assert "id" not in as_object(args["payload"])


# ---------------------------------------------------------------------------
# The journey's arguments against the committed schemas
# ---------------------------------------------------------------------------
# What nothing here checked before: the journey builds arguments by hand, and the
# only thing that ever proved them right was a live Store run. So when UCP
# 2026-08-25 changed `payload` from a JSON *string* to an object, every builder
# kept passing json.dumps() and every unit test stayed green — the failure was a
# rejected first cart mutation in CI, with the rest of both journeys skipping
# behind it. This reads the types straight off the snapshot instead.
SCHEMA_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "string": str,
    "boolean": bool,
    "array": list,
    "integer": int,
    "number": (int, float),
}

# Enough context to build every step's arguments. line_item_ids is a list because
# the fulfillment block indexes it; the rest only need to be present.
FULL_CTX: JsonObject = {
    **dict.fromkeys(("product_id", "cart_id", "checkout_id", "order_id", "promo_code", "context_token"), "x"),
    "line_item_ids": ["li-1"],
}


def _store_schemas() -> dict[str, JsonObject]:
    import json as _json
    from pathlib import Path as _Path

    snapshot = _Path(__file__).resolve().parents[1] / "tool-history" / "store.json"
    if not snapshot.exists():
        pytest.skip("tool-history/store.json not committed yet")
    tools = as_list(as_object(cast(object, _json.loads(snapshot.read_text()))).get("tools"))
    return {str(as_object(t).get("name")): as_object(as_object(t).get("inputSchema")) for t in tools}


@pytest.mark.parametrize("step", journeys.UCP_JOURNEY, ids=[s.tool for s in journeys.UCP_JOURNEY])
def test_every_journey_argument_matches_the_committed_schema(step: journeys.JourneyStep) -> None:
    schemas = _store_schemas()
    assert step.tool in schemas, f"{step.tool} is not in the Store catalogue"
    properties = as_object(schemas[step.tool].get("properties"))

    for name, value in step.args(FULL_CTX).items():
        assert name in properties, f"{step.tool} sends {name!r}, which its schema does not declare"
        declared = str(as_object(properties[name]).get("type", ""))
        expected = SCHEMA_TYPES.get(declared)
        if expected is None:
            continue
        assert isinstance(value, expected), (
            f"{step.tool}.{name} is declared {declared} but the journey sends {type(value).__name__}"
        )


def test_mutating_steps_are_explicit_about_committing() -> None:
    """dryRun cannot be used here — the plugin rolls each call back, so a dry-run
    cart-create hands the next step an id for a cart that no longer exists."""
    for step in journeys.UCP_JOURNEY:
        args = step.args(dict.fromkeys(("product_id", "cart_id", "checkout_id", "order_id", "promo_code"), "x"))
        if step.commits:
            assert args.get("dryRun") is False, f"{step.tool} claims to commit but sends dryRun={args.get('dryRun')}"
        else:
            assert "dryRun" not in args, f"{step.tool} is a read but sends dryRun"


@pytest.mark.parametrize("tool", ["search_catalog", "create_cart"])
def test_the_journey_covers_the_tools_the_store_fixtures_grade(tool: str) -> None:
    assert any(step.tool == tool for step in journeys.UCP_JOURNEY)


def _stub_transport(monkeypatch: pytest.MonkeyPatch, responses: dict[str, JsonObject]) -> list[tuple[str, JsonObject]]:
    """Drive the journey with canned tool responses, keyed by tool name."""
    seen: list[tuple[str, JsonObject]] = []

    def fake_call(_session: str, tool: str, args: JsonObject, endpoint: Endpoint | None = None) -> McpResponse:
        assert endpoint is None or endpoint is STORE
        seen.append((tool, args))
        # `line_items` on every default body, because a real cart or checkout
        # response is required to carry them with ids, and the journey reads those
        # ids to build fulfillment.methods[].line_item_ids. Without them the
        # checkout-update step skips on a missing precondition and takes the rest
        # of the flow with it — which is a fake-shaped failure, not a real one.
        body = responses.get(
            tool,
            {
                "success": True,
                "data": {
                    "id": f"{tool}-id",
                    "line_items": [{"id": f"{tool}-li-1", "item": {"id": "prod-1"}, "quantity": 1}],
                },
            },
        )
        return {"result": {"content": [{"type": "text", "text": json.dumps(body)}]}}

    def result_text(resp: McpResponse) -> str:
        blocks = (resp.get("result") or {}).get("content") or []
        return blocks[0].get("text", "") if blocks else ""

    monkeypatch.setattr(journeys, "mcp_call", fake_call)
    monkeypatch.setattr(journeys, "mcp_result_text", result_text)
    return seen


SEARCH_OK: JsonObject = {"success": True, "data": {"products": [{"id": "prod-1", "title": "A Guitar"}]}}

# What a conformant server answers a guest order read with, and what the guest
# journey therefore has to see to pass. `code` and `severity` are the assertion;
# the prose is here only to look like the real thing.
ORDER_REFUSED: JsonObject = {
    "success": False,
    "error": {
        "type": "not_found",
        "code": "not_found",
        "severity": "unrecoverable",
        "message": 'Order "o" is not available to this request. Use the permalink_url returned by checkout.complete.',
    },
}

GUEST_OK: dict[str, JsonObject] = {
    "search_catalog": SEARCH_OK,
    "get_order": ORDER_REFUSED,
}


def test_a_full_pass_threads_ids_and_records_every_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_transport(monkeypatch, dict(GUEST_OK))
    rep = _reporter()

    ctx = journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    assert ctx["product_id"] == "prod-1"
    assert ctx["cart_id"] == "create_cart-id"
    assert ctx["checkout_id"] == "create_checkout-id"
    assert rep.failed == 0
    # discount-apply is the only step without configuration, so it skips.
    assert rep.skipped == 1
    called = {tool for tool, _ in seen}
    assert "get_order" in called, "the journey never reached the order"


def test_an_in_band_failure_is_a_failure_and_stops_its_dependants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure mode the whole exercise exists for: HTTP 200, no JSON-RPC
    error, `success: false` in the body."""
    cart_create_rejected: JsonObject = {
        "success": False,
        "error": {"type": "validation", "message": "nope", "violations": ["$.line_items is required"]},
    }
    _stub_transport(monkeypatch, GUEST_OK | {"create_cart": cart_create_rejected})
    rep = _reporter()

    ctx = journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    assert "cart_id" not in ctx
    failures = [r for r in rep.records if r["status"] == "fail"]
    assert len(failures) == 1 and failures[0]["tool"] == "create_cart"
    assert "$.line_items is required" in failures[0].get("error", ""), "violations must survive to the report"
    skipped = {r["tool"] for r in rep.records if r["status"] == "skipped"}
    assert "get_cart" in skipped and "update_cart" in skipped


CUSTOMER = journeys.Persona("customer", "ctx-token-42")


def test_a_guest_passes_by_being_refused_the_order_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The order spec's MUST: a business authenticates order reads, and a
    platform credential does not authenticate the session that placed a guest
    order. So the refusal is the correct answer and the journey grades it as one."""
    _stub_transport(monkeypatch, dict(GUEST_OK))
    rep = _reporter()

    journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    read = next(r for r in rep.records if r["tool"] == journeys.ORDER_GET)
    assert read["status"] == "pass"
    assert "not_found/unrecoverable" in read.get("preview", "")


def test_a_guest_order_read_that_succeeds_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a shared, semi-public sales-channel key, serving the order means any
    key holder can read any order by id. A green here would report that as
    working."""
    _stub_transport(monkeypatch, {"search_catalog": SEARCH_OK})
    rep = _reporter()

    journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    read = next(r for r in rep.records if r["tool"] == journeys.ORDER_GET)
    assert read["status"] == "fail"
    assert "readable by id" in read.get("error", ""), "the report has to say what the success costs"


def test_the_refusal_is_graded_on_code_and_severity_not_on_being_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure this exists for: Shopware's own `Customer is not logged in.`
    403, which reaches the agent as invalid_request/recoverable and tells it to
    retry something no retry can fix (agentic-commerce#162). It is an error, so a
    suite checking only "did it fail" would have passed it for a year."""
    _stub_transport(
        monkeypatch,
        {
            "search_catalog": SEARCH_OK,
            journeys.ORDER_GET: {
                "success": False,
                "error": {
                    "type": "validation",
                    "code": "invalid_request",
                    "severity": "recoverable",
                    "message": "Customer is not logged in.",
                },
            },
        },
    )
    rep = _reporter()

    journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    read = next(r for r in rep.records if r["tool"] == journeys.ORDER_GET)
    assert read["status"] == "fail"
    assert "invalid_request/recoverable" in read.get("error", "")
    assert "not_found/unrecoverable" in read.get("error", ""), "the report has to name what was expected"


def test_a_customer_must_read_their_own_order_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: authenticated, the read has to work. Nothing else in the
    suite proves order-get ever returns an order."""
    _stub_transport(monkeypatch, {"search_catalog": SEARCH_OK})
    rep = _reporter()

    journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True, persona=CUSTOMER)

    read = next(r for r in rep.records if r["tool"] == journeys.ORDER_GET)
    assert read["status"] == "pass"
    assert all(step.refusal is None for step in journeys.journey_for(CUSTOMER))


def test_a_customer_anchors_the_checkout_to_their_own_context_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured, and the whole reason the customer half is not just the guest half
    with a header: `createCheckout` uses `cartId ?? generate()` as its Shopware
    context token. Without `cart_id` the checkout gets a fresh anonymous context
    and the order is placed for a guest registered on the spot, however the
    caller is logged in — every step still passes, and the order belongs to
    somebody who does not exist."""
    seen = _stub_transport(monkeypatch, {"search_catalog": SEARCH_OK})

    journeys.run_ucp_journey(_reporter(), "sid", STORE, allow_mutations=True, persona=CUSTOMER)

    create = next(args for tool, args in seen if tool == "create_checkout")
    assert as_object(create["payload"])["cart_id"] == CUSTOMER.context_token


def test_a_guest_sends_no_cart_id_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """checkout.create takes line items directly and has no cart reference — the
    opposite of what the tool names suggest, so the guest flow pins it."""
    seen = _stub_transport(monkeypatch, dict(GUEST_OK))

    journeys.run_ucp_journey(_reporter(), "sid", STORE, allow_mutations=True)

    create = next(args for tool, args in seen if tool == "create_checkout")
    assert "cart_id" not in as_object(create["payload"])


def test_both_personas_send_the_same_requests_apart_from_the_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a difference in outcome is attributable to the suite rather than
    to the server, which is the one thing the comparison is for."""
    guest_seen = _stub_transport(monkeypatch, dict(GUEST_OK))
    journeys.run_ucp_journey(_reporter(), "sid", STORE, allow_mutations=True)
    guest = [(tool, args) for tool, args in guest_seen]

    customer_seen = _stub_transport(monkeypatch, {"search_catalog": SEARCH_OK})
    journeys.run_ucp_journey(_reporter(), "sid", STORE, allow_mutations=True, persona=CUSTOMER)

    assert [tool for tool, _ in guest] == [tool for tool, _ in customer_seen]
    differing = [
        tool
        for (tool, guest_args), (_, customer_args) in zip(guest, customer_seen, strict=True)
        if guest_args != customer_args
    ]
    assert differing == ["create_checkout"]


def _ordered_context() -> JsonObject:
    """A context as the customer journey leaves it: one order already placed."""
    return {"product_id": "prod-1", "context_token": CUSTOMER.context_token, "order_id": "order-1"}


def test_a_second_order_repeats_only_the_checkout_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalogue and cart tools were proven by the journey; repeating them would
    add duplicate records and prove nothing new."""
    seen = _stub_transport(
        monkeypatch,
        {"complete_checkout": {"success": True, "data": {"order": {"id": "order-2"}}}},
    )
    rep = _reporter()

    journeys.run_second_order(rep, "sid", STORE, _ordered_context())

    assert [tool for tool, _ in seen] == list(journeys.SECOND_ORDER_STEPS)
    assert rep.failed == 0
    assert rep.passed == 1


def test_a_second_order_runs_on_the_same_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting a fresh session would test the workaround instead of the thing that
    is broken. The point is that the buyer who just ordered can order again."""
    seen = _stub_transport(
        monkeypatch,
        {"complete_checkout": {"success": True, "data": {"order": {"id": "order-2"}}}},
    )

    journeys.run_second_order(_reporter(), "sid", STORE, _ordered_context())

    create = next(args for tool, args in seen if tool == "create_checkout")
    assert as_object(create["payload"])["cart_id"] == CUSTOMER.context_token


def test_the_refusal_a_returning_buyer_actually_gets_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the live lane answers today (O12): the checkout id doubles as the context
    token, a completed id is spent permanently, and a signed-in buyer's token never
    changes — so the second order is refused."""
    _stub_transport(
        monkeypatch,
        {
            "update_checkout": {
                "success": False,
                "error": {
                    "type": "validation",
                    "code": "invalid_request",
                    "severity": "recoverable",
                    "message": "Completed checkout sessions cannot be updated.",
                },
            }
        },
    )
    rep = _reporter()

    journeys.run_second_order(rep, "sid", STORE, _ordered_context())

    failure = next(r for r in rep.records if r["status"] == "fail")
    assert failure["tool"] == "check", "the fault is in the session lifecycle, not in the tool"
    assert "cannot be updated" in failure.get("error", "")


def test_a_replayed_order_id_does_not_count_as_a_second_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same failure wearing a success: completion answered from the first order's
    record instead of placing a new one, and the ids give it away."""
    _stub_transport(
        monkeypatch,
        {"complete_checkout": {"success": True, "data": {"order": {"id": "order-1"}}}},
    )
    rep = _reporter()

    journeys.run_second_order(rep, "sid", STORE, _ordered_context())

    assert rep.failed == 1
    assert any("order-1 again" in r.get("error", "") for r in rep.records)


def test_a_second_order_is_not_attempted_without_a_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A journey that never reached an order has nothing to repeat, and saying
    "precondition missing" points at where it actually stopped."""
    seen = _stub_transport(monkeypatch, {})
    rep = _reporter()

    journeys.run_second_order(rep, "sid", STORE, {"context_token": CUSTOMER.context_token})

    assert seen == []
    assert any("precondition missing" in r.get("error", "") for r in rep.records)


def test_the_promo_code_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(journeys.PROMO_CODE_ENV, "SAVE15")
    seen = _stub_transport(monkeypatch, dict(GUEST_OK))

    journeys.run_ucp_journey(_reporter(), "sid", STORE, allow_mutations=True)

    discount = next(args for tool, args in seen if tool == "apply_discount")
    assert discount["code"] == "SAVE15"


def test_an_unreadable_response_fails_rather_than_passing_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    reply: McpResponse = {"result": {"content": [{"text": "not json"}]}}
    monkeypatch.setattr(journeys, "mcp_call", const(reply))
    monkeypatch.setattr(journeys, "mcp_result_text", const("not json"))
    rep = _reporter()

    journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    assert rep.failed >= 1
    assert any("not readable" in r.get("error", "") for r in rep.records)
