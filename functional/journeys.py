#!/usr/bin/env python3
"""The UCP buyer journey, tested as one flow rather than thirteen tools.

Calling the UCP tools in isolation proves almost nothing. `cart-get` needs an id
that only `cart-create` can produce, `checkout-*` needs a checkout, `order-get`
needs a completed order — so a table of independent calls with invented ids
mostly measures how the server words "not found". That is very likely most of
what the Store suite has been reporting.

Dry-run does not rescue it either, and this is the part worth understanding: the
plugin runs each mutating call inside a transaction it always rolls back, so a
dry-run `cart-create` returns a plausible cart id belonging to a cart that no
longer exists. The next step then fails on a perfectly well-formed request. A
chained journey has to commit.

So it commits, and that is guarded: `--allow-mutations` is off by default and the
journey is skipped with a recorded reason when it is absent. This is the only
place the suite writes real state, and it must be impossible to point at a
developer's own shop by accident.

What each step buys us:

  * every step's assertion is the next step's precondition, so a break is
    *located* rather than merely counted — a failure at `checkout-update` with
    `cart-create` green is a different bug report than both failing;
  * the ids are real, so "not found" becomes a genuine failure rather than the
    expected outcome it is today;
  * the argument shapes are exercised for real. Three of them are not guessable
    from the schema and were found by hand against a live lane — see PAYLOAD
    NOTES below.

PAYLOAD NOTES (measured, not documented anywhere the model can see):
  * line items are `{"item": {"id": …}, "quantity": n}`. A flat `{"id": …}` is
    rejected by the schema.
  * `checkout.update` requires the *whole* `line_items` array again — it is PUT,
    not PATCH. An agent told to "add a shipping address" will send only the
    address and fail every single time.
  * `payload` is a JSON object *string*, and the schema's own declared default
    `"{}"` fails its own validation.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from eval.assertions import inband_error
from functional.reporting import Reporter
from mcp_client import Endpoint, mcp_call, mcp_result_text

# A buyer who is obviously synthetic, so a leftover order in a dev shop is
# identifiable at a glance rather than looking like a real customer.
BUYER = {"email": "mcp-evals@example.invalid", "first_name": "MCP", "last_name": "Evals"}
ADDRESS = {
    "name": "MCP Evals",
    "line_one": "Evaluation Street 1",
    "city": "Berlin",
    "postal_code": "10115",
    "country": "DE",
}

# A promotion code to exercise discount-apply. There is no reliable way to
# discover one from the Store API alone, and demo data differs per shop, so this
# is configuration rather than a guess — absent, the step skips with a reason
# instead of failing on a code that was never going to exist.
PROMO_CODE_ENV = "UCP_JOURNEY_PROMO_CODE"

# checkout.complete refuses a checkout without `payment`, and checkout-complete
# takes only an id — so this has to be set during update or the order can never
# be placed. Measured: the contents are not validated at all, an empty object is
# accepted, and no payment_handlers are advertised in discovery. So this is the
# shape an agent would plausibly send, not a value the server recognises.
PAYMENT = {"method": "invoice"}

# A step that takes longer than this is reported as slow. Not a failure on its
# own: it is the signal that separates "the server rejected us" from "the server
# went away and thought about it", which is the difference between a config
# problem and a hang.
SLOW_STEP_S = 5.0


@dataclass(frozen=True)
class JourneyStep:
    """One hop in the flow.

    `needs` names context keys an earlier step must have produced. A step whose
    preconditions are missing is skipped with the reason naming what did not
    arrive, so one early break does not cascade into a wall of red that hides
    where it started.
    """

    tool: str
    detail: str
    args: Callable[[dict], dict]
    capture: Callable[[dict, dict], None] = field(default=lambda payload, ctx: None)
    needs: tuple[str, ...] = ()
    commits: bool = False

    def missing(self, ctx: dict) -> str:
        return next((key for key in self.needs if not ctx.get(key)), "")


def _line_items(ctx: dict) -> list[dict]:
    return [{"item": {"id": ctx["product_id"]}, "quantity": 1}]


UCP_JOURNEY: tuple[JourneyStep, ...] = (
    JourneyStep(
        tool="shopware-ucp-catalog-search",
        detail="find a product to buy",
        args=lambda ctx: {"query": ctx.get("query", ""), "limit": 5},
        capture=lambda payload, ctx: ctx.update(
            product_id=(payload.get("products") or [{}])[0].get("id", ""),
            product_title=(payload.get("products") or [{}])[0].get("title", ""),
        ),
    ),
    JourneyStep(
        tool="shopware-ucp-catalog-lookup",
        detail="look the product up by id",
        # `ids` is a string, not an array — the one parameter shape an agent gets
        # wrong most often, so the journey pins the accepted form.
        args=lambda ctx: {"ids": ctx["product_id"]},
        needs=("product_id",),
    ),
    JourneyStep(
        tool="shopware-ucp-cart-create",
        detail="open a cart with that product",
        args=lambda ctx: {"payload": json.dumps({"line_items": _line_items(ctx)}), "dryRun": False},
        capture=lambda payload, ctx: ctx.update(cart_id=payload.get("id", "")),
        needs=("product_id",),
        commits=True,
    ),
    JourneyStep(
        tool="shopware-ucp-cart-update",
        detail="change the quantity",
        # `id` goes in the payload as well as the tool argument. The tool takes
        # `id` as a required parameter and then rejects the request for `$.id is
        # required` — the same value, needed twice, in two places.
        args=lambda ctx: {
            "id": ctx["cart_id"],
            "payload": json.dumps(
                {"id": ctx["cart_id"], "line_items": [{"item": {"id": ctx["product_id"]}, "quantity": 2}]}
            ),
            "dryRun": False,
        },
        needs=("cart_id", "product_id"),
        commits=True,
    ),
    JourneyStep(
        tool="shopware-ucp-cart-get",
        detail="read the cart back",
        args=lambda ctx: {"id": ctx["cart_id"]},
        needs=("cart_id",),
    ),
    JourneyStep(
        tool="shopware-ucp-discount-apply",
        detail="apply a promotion code",
        args=lambda ctx: {"cartId": ctx["cart_id"], "code": ctx["promo_code"], "dryRun": False},
        needs=("cart_id", "promo_code"),
        commits=True,
    ),
    JourneyStep(
        tool="shopware-ucp-checkout-create",
        detail="start a checkout",
        # Deliberately not derived from the cart: checkout.create takes line
        # items directly and has no cart reference at all, which is worth pinning
        # because it is the opposite of what the tool names suggest.
        args=lambda ctx: {"payload": json.dumps({"line_items": _line_items(ctx)}), "dryRun": False},
        capture=lambda payload, ctx: ctx.update(checkout_id=payload.get("id", "")),
        needs=("product_id",),
        commits=True,
    ),
    JourneyStep(
        tool="shopware-ucp-checkout-update",
        detail="add buyer and address",
        # line_items again: update is PUT, not PATCH. `payment` is required for
        # the checkout to reach ready_for_complete, and is where it has to be set
        # — checkout-complete takes only an id, so there is no later opportunity.
        # Its contents are not validated (an empty object works), so this carries
        # the shape an agent would plausibly send rather than a magic value.
        args=lambda ctx: {
            "id": ctx["checkout_id"],
            "payload": json.dumps(
                {
                    "line_items": _line_items(ctx),
                    "buyer": BUYER,
                    "fulfillment_address": ADDRESS,
                    "payment": PAYMENT,
                }
            ),
            "dryRun": False,
        },
        needs=("checkout_id", "product_id"),
        commits=True,
    ),
    JourneyStep(
        tool="shopware-ucp-checkout-complete",
        detail="place the order",
        args=lambda ctx: {"id": ctx["checkout_id"], "dryRun": False},
        capture=lambda payload, ctx: ctx.update(order_id=payload.get("order", {}).get("id") or payload.get("id", "")),
        needs=("checkout_id",),
        commits=True,
    ),
    JourneyStep(
        tool="shopware-ucp-order-get",
        detail="read the placed order back",
        args=lambda ctx: {"id": ctx["order_id"]},
        needs=("order_id",),
    ),
    JourneyStep(
        tool="shopware-ucp-cart-cancel",
        detail="cancel the cart",
        args=lambda ctx: {"id": ctx["cart_id"], "dryRun": False},
        needs=("cart_id",),
        commits=True,
    ),
)


def _call(session: str, endpoint: Endpoint, step: JourneyStep, ctx: dict) -> tuple[float, str, dict]:
    """Run one step. Returns (elapsed, error, payload)."""
    started = time.monotonic()
    response = mcp_call(session, step.tool, step.args(ctx), endpoint=endpoint)
    elapsed = time.monotonic() - started
    text = mcp_result_text(response)
    error = response.get("error", {}).get("message", "") or inband_error(text) or ""
    if error:
        return elapsed, error, {}
    try:
        payload = json.loads(text).get("data", {})
    except ValueError, TypeError:
        return elapsed, "response was not readable JSON", {}
    return elapsed, "", payload if isinstance(payload, dict) else {}


def run_ucp_journey(
    rep: Reporter,
    session: str,
    endpoint: Endpoint,
    allow_mutations: bool = False,
    query: str = "",
) -> dict:
    """Walk the buyer journey. Returns the context gathered along the way.

    Every step commits, so the whole journey is gated on `allow_mutations`. It is
    reported as skipped rather than silently omitted: a suite that quietly does
    less than it claims is worse than one that fails.
    """
    steps = UCP_JOURNEY
    if not allow_mutations:
        for step in steps:
            rep.tool_skip(
                step.tool,
                f"{step.tool} ({step.detail})",
                "journey needs --allow-mutations: it places a real order",
            )
        return {}

    ctx: dict = {"query": query}
    if code := os.environ.get(PROMO_CODE_ENV, ""):
        ctx["promo_code"] = code

    for step in steps:
        label = f"{step.tool} ({step.detail})"
        if missing := step.missing(ctx):
            # Name the precondition, not the symptom. "needs cart_id" points at
            # the step that should have produced it; "not found" does not.
            rep.tool_skip(step.tool, label, f"precondition missing: {missing}")
            continue

        elapsed, error, payload = _call(session, endpoint, step, ctx)
        if error:
            rep.tool_fail(step.tool, label, f"{error} [{elapsed:.1f}s]")
            continue

        step.capture(payload, ctx)
        note = f"{elapsed:.1f}s"
        if elapsed > SLOW_STEP_S:
            note += " — slow"
        rep.tool_pass(step.tool, label, note)

    return ctx
