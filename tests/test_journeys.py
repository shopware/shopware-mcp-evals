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

from eval.result_schema import JsonObject, McpResponse, as_object
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
        step.capture({"id": "x", "products": [{"id": "p", "title": "t"}], "order": {"id": "o"}}, ctx)
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
    step = next(s for s in journeys.UCP_JOURNEY if s.tool == "shopware-ucp-checkout-update")
    payload = as_object(cast(object, json.loads(str(step.args({"checkout_id": "c", "product_id": "p"})["payload"]))))

    assert payload["line_items"], "update dropped line_items"
    assert "buyer" in payload and "fulfillment_address" in payload
    assert "payment" in payload, "without payment the checkout never reaches ready_for_complete"


def test_cart_update_repeats_the_id_inside_the_payload() -> None:
    """The tool takes `id` as a required parameter and then rejects the request
    for `$.id is required` — the same value, needed twice, in two places."""
    step = next(s for s in journeys.UCP_JOURNEY if s.tool == "shopware-ucp-cart-update")
    args = step.args({"cart_id": "cart-1", "product_id": "p"})

    assert args["id"] == "cart-1"
    assert as_object(cast(object, json.loads(str(args["payload"]))))["id"] == "cart-1"


def test_mutating_steps_are_explicit_about_committing() -> None:
    """dryRun cannot be used here — the plugin rolls each call back, so a dry-run
    cart-create hands the next step an id for a cart that no longer exists."""
    for step in journeys.UCP_JOURNEY:
        args = step.args(dict.fromkeys(("product_id", "cart_id", "checkout_id", "order_id", "promo_code"), "x"))
        if step.commits:
            assert args.get("dryRun") is False, f"{step.tool} claims to commit but sends dryRun={args.get('dryRun')}"
        else:
            assert "dryRun" not in args, f"{step.tool} is a read but sends dryRun"


@pytest.mark.parametrize("tool", ["shopware-ucp-catalog-search", "shopware-ucp-cart-create"])
def test_the_journey_covers_the_tools_the_store_fixtures_grade(tool: str) -> None:
    assert any(step.tool == tool for step in journeys.UCP_JOURNEY)


def _stub_transport(monkeypatch: pytest.MonkeyPatch, responses: dict[str, JsonObject]) -> list[tuple[str, JsonObject]]:
    """Drive the journey with canned tool responses, keyed by tool name."""
    seen: list[tuple[str, JsonObject]] = []

    def fake_call(_session: str, tool: str, args: JsonObject, endpoint: Endpoint | None = None) -> McpResponse:
        assert endpoint is None or endpoint is STORE
        seen.append((tool, args))
        body = responses.get(tool, {"success": True, "data": {"id": f"{tool}-id"}})
        return {"result": {"content": [{"type": "text", "text": json.dumps(body)}]}}

    def result_text(resp: McpResponse) -> str:
        blocks = (resp.get("result") or {}).get("content") or []
        return blocks[0].get("text", "") if blocks else ""

    monkeypatch.setattr(journeys, "mcp_call", fake_call)
    monkeypatch.setattr(journeys, "mcp_result_text", result_text)
    return seen


SEARCH_OK: JsonObject = {"success": True, "data": {"products": [{"id": "prod-1", "title": "A Guitar"}]}}


def test_a_full_pass_threads_ids_and_records_every_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_transport(monkeypatch, {"shopware-ucp-catalog-search": SEARCH_OK})
    rep = _reporter()

    ctx = journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    assert ctx["product_id"] == "prod-1"
    assert ctx["cart_id"] == "shopware-ucp-cart-create-id"
    assert ctx["checkout_id"] == "shopware-ucp-checkout-create-id"
    assert rep.failed == 0
    # discount-apply is the only step without configuration, so it skips.
    assert rep.skipped == 1
    called = {tool for tool, _ in seen}
    assert "shopware-ucp-order-get" in called, "the journey never reached the order"


def test_an_in_band_failure_is_a_failure_and_stops_its_dependants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure mode the whole exercise exists for: HTTP 200, no JSON-RPC
    error, `success: false` in the body."""
    _stub_transport(
        monkeypatch,
        {
            "shopware-ucp-catalog-search": SEARCH_OK,
            "shopware-ucp-cart-create": {
                "success": False,
                "error": {"type": "validation", "message": "nope", "violations": ["$.line_items is required"]},
            },
        },
    )
    rep = _reporter()

    ctx = journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    assert "cart_id" not in ctx
    failures = [r for r in rep.records if r["status"] == "fail"]
    assert len(failures) == 1 and failures[0]["tool"] == "shopware-ucp-cart-create"
    assert "$.line_items is required" in failures[0].get("error", ""), "violations must survive to the report"
    skipped = {r["tool"] for r in rep.records if r["status"] == "skipped"}
    assert "shopware-ucp-cart-get" in skipped and "shopware-ucp-cart-update" in skipped


def test_the_promo_code_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(journeys.PROMO_CODE_ENV, "SAVE15")
    seen = _stub_transport(monkeypatch, {"shopware-ucp-catalog-search": SEARCH_OK})

    journeys.run_ucp_journey(_reporter(), "sid", STORE, allow_mutations=True)

    discount = next(args for tool, args in seen if tool == "shopware-ucp-discount-apply")
    assert discount["code"] == "SAVE15"


def test_an_unreadable_response_fails_rather_than_passing_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    reply: McpResponse = {"result": {"content": [{"text": "not json"}]}}
    monkeypatch.setattr(journeys, "mcp_call", const(reply))
    monkeypatch.setattr(journeys, "mcp_result_text", const("not json"))
    rep = _reporter()

    journeys.run_ucp_journey(rep, "sid", STORE, allow_mutations=True)

    assert rep.failed >= 1
    assert any("not readable" in r.get("error", "") for r in rep.records)
