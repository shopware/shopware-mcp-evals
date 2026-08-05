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

The flow runs twice, as a guest and as a logged-in customer (see Persona). Both
send byte-identical payloads apart from the one field that anchors a checkout to
a customer session, so where the two outcomes differ, the difference is the
server's and not the suite's. It is also where the interesting half of the
checkout lives: a customer reads their own order back, and a guest is refused —
and asserting that refusal is worth more than asserting the read, because it is
the half the specification has a MUST about.

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
from dataclasses import dataclass, field, replace
from typing import cast

from eval.assertions import inband_error
from eval.result_schema import JsonObject, as_list, as_object
from functional.reporting import Reporter
from mcp_client import Endpoint, mcp_call, mcp_result_text

# The ids each step contributes and the later steps consume, keyed by the names
# `needs` refers to. Distinct from functional.checks.Context — same shape, but
# this one is built by the journey itself rather than gathered up front.
type Context = JsonObject

# A buyer who is obviously synthetic, so a leftover order in a dev shop is
# identifiable at a glance rather than looking like a real customer.
BUYER = {"email": "mcp-evals@example.invalid", "first_name": "MCP", "last_name": "Evals"}
# UCP postal-address field names, which are schema.org's and not Shopware's.
# Checked against the generated 2026-04-08 schema: the address object accepts
# street_address, extended_address, address_locality, address_region,
# postal_code, address_country, first_name, last_name and phone_number — so the
# `line_one` / `city` / `country` / `name` spelling this used to carry matched
# nothing and was silently dropped.
ADDRESS = {
    "street_address": "Evaluation Street 1",
    "address_locality": "Berlin",
    "postal_code": "10115",
    "address_country": "DE",
    "first_name": "MCP",
    "last_name": "Evals",
}

# The id the journey gives its destination. Any string works; naming it after the
# suite keeps a stray checkout session identifiable in a dev shop.
DESTINATION_ID = "mcp-evals-destination"

DESTINATION_SHAPE = """Exactly one destination shape validates, and it needs an `id` and NO `name`.

The item is a `oneOf`. Branch 0, shipping_destination, is an allOf of
postal_address AND `{properties: {id}, required: [id]}` — so it needs an `id`.
Branch 1, retail_location, requires `id` AND `name`. Measured against the
installed validator (GeneratedSchemaValidator, checkout.update.request,
ucp-php-sdk 0.0.3):

    [<bare postal address>]         -> INVALID   no id, so neither branch matches
    [<address, no first/last name>] -> INVALID   same reason
    [{id, name, address}]           -> INVALID   matches BOTH branches
    [{id, name}]                    -> INVALID   matches BOTH branches
    [{id, ...postal address}]       -> VALID     branch 0 only

Adding `name` is what makes an object ambiguous, so the shape below carries an
`id` and never a `name`. An earlier reading of this called the oneOf
unsatisfiable and omitted `destinations` entirely, on the strength of the first
three rows — the fourth combination was never tried, and the plugin's
"Checkout session is missing fulfillment.shipping_address" looked like
confirmation. It was not: that message named a field which is not a property of
checkout.create, checkout.update or checkout.complete in any UCP version, and
the plugin has since been fixed to read the destination and to say so.
"""

# A promotion code to exercise discount-apply. There is no reliable way to
# discover one from the Store API alone, and demo data differs per shop, so this
# is configuration rather than a guess — absent, the step skips with a reason
# instead of failing on a code that was never going to exist.
PROMO_CODE_ENV = "UCP_JOURNEY_PROMO_CODE"

# checkout.update accepts `payment` and the checkout reaches ready_for_complete,
# but checkout-get shows it was never persisted. It does not need to be:
# UcpCheckoutCompleteTool fills in `payment: {instruments: []}` when the agent
# omits one (UcpCheckoutCompletionPayment::apply, plugin #155), which is what
# `checkout.complete.request` requires, and completion charges the sales channel
# default. This block stays because it is what an agent would plausibly send.
#
# Still true and worth knowing: the no-input handler that satisfies this —
# ShopwareInvoicePaymentHandler, `com.shopware.invoice`, tokenization false —
# exists but discovery advertises `payment_handlers: {}`, so an agent cannot find
# it. Nothing here depends on that, since omitting payment works.
PAYMENT = {"method": "invoice"}

# A step that takes longer than this is reported as slow. Not a failure on its
# own: it is the signal that separates "the server rejected us" from "the server
# went away and thought about it", which is the difference between a config
# problem and a hang.
SLOW_STEP_S = 5.0


@dataclass(frozen=True)
class Refusal:
    """A step whose *refusal* is the pass condition, and the shape it must take.

    `types/message_error.json` requires `code` and `severity` on every error, and
    those two are the whole assertion: they are what an agent branches on. The
    prose is not checked, because a server is free to reword it and a suite that
    pinned the wording would fail on an improvement.
    """

    code: str
    severity: str


@dataclass(frozen=True)
class Persona:
    """Who the journey is shopping as.

    The two personas are not two payloads — they are the same requests made with
    and without a Shopware customer session, which is the only difference an
    agent can actually control:

      * **guest** — no context token. Every UCP write mints its own anonymous
        Shopware context, `buyer.email` identifies the buyer, and the plugin
        registers a guest customer at completion.
      * **customer** — a context token from a Store API login, passed as
        `sw-context-token` AND as `cart_id` on checkout.create.

    That second half is the part worth knowing, and it was measured rather than
    read: `ShopwareCheckoutAdapter::createCheckout` uses `cartId ?? generate()`
    as its Shopware context token, and `ShopwareCartAdapter::createCart`
    generates one unconditionally. So the header alone does NOT reach the
    checkout — with it and without `cart_id`, the whole journey still passes and
    the order lands on a freshly registered *guest* (verified in the database:
    `customer.guest = 1`, the buyer-block email), while `order.get` — the one
    operation that does read the incoming header — looks in the real customer's
    orders and finds nothing. Passing the token as `cart_id` anchors the checkout
    to that context, and then the order is the customer's own
    (`customer.guest = 0`) and reads back.

    A UCP cart id IS a Shopware context token here (cart.create returns the
    token it generated as the cart's id), so this is coherent with the plugin's
    own model rather than a trick — but nothing in discovery says so, which is
    worth reporting on its own.
    """

    name: str
    context_token: str = ""

    @property
    def authenticated(self) -> bool:
        return bool(self.context_token)


GUEST = Persona("guest")

# What a guest order read must be refused with, and why a refusal is the correct
# answer rather than a gap:
#
#   the order specification requires one — "the business MUST authenticate
#   requests to order data before returning a response" — and only *permits* the
#   case a UCP agent is in here, for businesses that "MAY allow access for orders
#   the platform originated". The only credential on the request is the
#   sales-channel access key, which is shared and semi-public, so serving the
#   order on its strength would let any key holder read any order by id.
#
# So the suite asserts the refusal, and a *success* here is a finding: it means
# that oracle is open. `not_found` rather than a forbidden-shaped code for the
# same reason — "exists but not yours" and "does not exist" have to be
# indistinguishable. Both values come from the plugin's own
# UcpErrorDescriptor mapping (agentic-commerce#162, finding O9); before it, this
# escaped as Shopware's `Customer is not logged in.` 403, reported as
# `invalid_request` / `recoverable`, telling the agent to retry something no
# retry can fix.
GUEST_ORDER_REFUSAL = Refusal("not_found", "unrecoverable")

ORDER_GET = "shopware-ucp-order-get"


@dataclass(frozen=True)
class JourneyStep:
    """One hop in the flow.

    `needs` names context keys an earlier step must have produced. A step whose
    preconditions are missing is skipped with the reason naming what did not
    arrive, so one early break does not cascade into a wall of red that hides
    where it started.

    `refusal`, when set, inverts the verdict: the step passes only if the server
    refuses it in that shape.
    """

    tool: str
    detail: str
    args: Callable[[Context], JsonObject]
    capture: Callable[[JsonObject, Context], None] = field(default=lambda payload, ctx: None)
    needs: tuple[str, ...] = ()
    commits: bool = False
    refusal: Refusal | None = None

    def missing(self, ctx: Context) -> str:
        return next((key for key in self.needs if not ctx.get(key)), "")


@dataclass(frozen=True)
class Outcome:
    """What one step's call produced.

    `error_body` is carried alongside the flattened `error` string because the
    refusal assertion is on `code` and `severity`, and flattening loses them.
    """

    elapsed: float
    error: str
    error_body: JsonObject
    payload: JsonObject


def _line_items(ctx: Context) -> list[JsonObject]:
    return [{"item": {"id": ctx["product_id"]}, "quantity": 1}]


def _line_item_ids(payload: JsonObject) -> list[str]:
    """The ids the server assigned to the checkout's line items.

    `id` is REQUIRED on a checkout response's line items (generated
    checkout.get.response, branch `dev.ucp.shopping.checkout`), so these are
    dependable rather than best-effort — which matters because
    `fulfillment.methods[].line_item_ids` is required and has to name real ones.
    """
    return [id for row in as_list(payload.get("line_items")) if (id := str(as_object(row).get("id", "")))]


def _fulfillment(ctx: Context) -> JsonObject:
    """The shipping destination, in the one place the schema puts it.

    `fulfillment.methods[].destinations[]` — not a top-level
    `fulfillment_address`, which is not a property of checkout.create/update at
    all. See DESTINATION_SHAPE for why the destination carries an `id` and no
    `name`; that is the only combination the oneOf accepts.

    `line_item_ids` is the one required field on a method, so this is built from
    the ids captured off checkout-create rather than invented.
    """
    return {
        "methods": [
            {
                "type": "shipping",
                "line_item_ids": as_list(ctx.get("line_item_ids")),
                "destinations": [{"id": DESTINATION_ID, **ADDRESS}],
                # Redundant with a single destination, but it is what an agent
                # offered several would send, so the plugin's selection path is
                # exercised rather than only its fallback to the first entry.
                "selected_destination_id": DESTINATION_ID,
            }
        ]
    }


def _checkout_create_payload(ctx: Context) -> str:
    """checkout.create's payload, anchored to the buyer's session when there is one.

    `cart_id` is what carries an authenticated customer into the checkout: the
    adapter uses it as the Shopware context token, and without it the checkout
    gets a fresh anonymous one and the order is placed for a guest no matter who
    the caller is logged in as. See Persona for the measurement.

    The guest journey sends no `cart_id` on purpose, and deliberately does not
    send the cart it created either — checkout.create takes line items directly
    and has no cart reference of its own, which is worth pinning because it is
    the opposite of what the tool names suggest.
    """
    payload: JsonObject = {"line_items": _line_items(ctx)}
    if token := str(ctx.get("context_token", "")):
        payload["cart_id"] = token

    return json.dumps(payload)


def _first(payload: JsonObject, key: str) -> JsonObject:
    """The first element of a list-valued payload key, as a map, or {}.

    Stands in for `(payload.get(key) or [{}])[0]`, which cannot be typed: the
    value is `object` until something narrows it.
    """
    rows = as_list(payload.get(key))
    return as_object(rows[0]) if rows else {}


UCP_JOURNEY: tuple[JourneyStep, ...] = (
    JourneyStep(
        tool="shopware-ucp-catalog-search",
        detail="find a product to buy",
        args=lambda ctx: {"query": ctx.get("query", ""), "limit": 5},
        capture=lambda payload, ctx: ctx.update(
            product_id=str(_first(payload, "products").get("id", "")),
            product_title=str(_first(payload, "products").get("title", "")),
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
        capture=lambda payload, ctx: ctx.update(cart_id=str(payload.get("id", ""))),
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
        args=lambda ctx: {"payload": _checkout_create_payload(ctx), "dryRun": False},
        # The line-item ids come back here and nowhere else the journey looks, and
        # the next step needs them for fulfillment.methods[].line_item_ids.
        capture=lambda payload, ctx: ctx.update(
            checkout_id=str(payload.get("id", "")),
            line_item_ids=_line_item_ids(payload),
        ),
        needs=("product_id",),
        commits=True,
    ),
    JourneyStep(
        tool="shopware-ucp-checkout-update",
        detail="add buyer, address and payment",
        # line_items again: update is PUT, not PATCH. `payment` is required for
        # the checkout to reach ready_for_complete, and is where it has to be set
        # — checkout-complete takes only an id and a payload of its own, so this
        # is the last chance to attach the shipping destination.
        args=lambda ctx: {
            "id": ctx["checkout_id"],
            "payload": json.dumps(
                {
                    "line_items": _line_items(ctx),
                    "buyer": BUYER,
                    "fulfillment": _fulfillment(ctx),
                    "payment": PAYMENT,
                }
            ),
            "dryRun": False,
        },
        # line_item_ids is a precondition, not an optional extra: without it the
        # fulfillment block would be sent with an empty required field, and the
        # journey would fail on our own malformed request rather than on anything
        # the server did. Skipping names the missing piece instead.
        needs=("checkout_id", "product_id", "line_item_ids"),
        commits=True,
    ),
    JourneyStep(
        tool="shopware-ucp-checkout-complete",
        detail="place the order",
        args=lambda ctx: {"id": ctx["checkout_id"], "dryRun": False},
        capture=lambda payload, ctx: ctx.update(
            order_id=str(as_object(payload.get("order")).get("id") or payload.get("id", ""))
        ),
        needs=("checkout_id",),
        commits=True,
    ),
    JourneyStep(
        tool=ORDER_GET,
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


# The steps a returning buyer repeats. Not the whole journey: the catalogue and the
# cart tools were already proven, and repeating them would only add duplicate records.
SECOND_ORDER_STEPS = (
    "shopware-ucp-checkout-create",
    "shopware-ucp-checkout-update",
    "shopware-ucp-checkout-complete",
)

SECOND_ORDER_CHECK = "a signed-in buyer can place a second order"


def run_second_order(rep: Reporter, session: str, endpoint: Endpoint, ctx: Context) -> None:
    """Order again on the session that just ordered — which is what buyers do.

    One order proves the checkout works once. This proves it is not a one-shot,
    and that distinction is not academic: the checkout id doubles as the Shopware
    context token, `CheckoutCompletionStore` marks a completed id spent
    permanently (so a repeated `checkout.complete` replays instead of charging
    twice), and Shopware hands a signed-in buyer the same token on every login.
    While those three hold, a buyer's second order is refused with `Completed
    checkout sessions cannot be updated.` and logging in again does not clear it.

    Reported as a check rather than as tool assertions on purpose. `checkout-update`
    is not broken — it works for every first order — so failing its health entry
    would suppress its fixtures and misname the fault, which is in the session
    lifecycle. Tracked as O12; see agentic-commerce#162 for the two designs that
    did not survive contact with it.

    Deliberately runs on the *same* context token the first order used, since
    replacing it is the workaround this check exists to not rely on.
    """
    first_order = str(ctx.get("order_id", ""))
    steps = [step for step in UCP_JOURNEY if step.tool in SECOND_ORDER_STEPS]

    for step in steps:
        if missing := step.missing(ctx):
            rep.check_fail(SECOND_ORDER_CHECK, f"precondition missing: {missing}")
            return

        outcome = _call(session, endpoint, step, ctx)
        if outcome.error:
            rep.check_fail(SECOND_ORDER_CHECK, f"{step.tool}: {outcome.error}")
            return

        step.capture(outcome.payload, ctx)

    second_order = str(ctx.get("order_id", ""))
    if not second_order or second_order == first_order:
        # A replayed order id is the same failure wearing a success: completion
        # answered from the first order's record instead of placing a new one.
        rep.check_fail(SECOND_ORDER_CHECK, f"the second checkout returned order {second_order or '<none>'} again")
        return

    rep.check_pass(f"{SECOND_ORDER_CHECK} ({second_order[:8]}… after {first_order[:8]}…)")


def _call(session: str, endpoint: Endpoint, step: JourneyStep, ctx: Context) -> Outcome:
    """Run one step."""
    started = time.monotonic()
    response = mcp_call(session, step.tool, step.args(ctx), endpoint=endpoint)
    elapsed = time.monotonic() - started
    text = mcp_result_text(response)
    error = (response.get("error") or {}).get("message", "") or inband_error(text) or ""
    if error:
        return Outcome(elapsed, error, _error_body(text), {})
    try:
        payload = as_object(cast(object, json.loads(text))).get("data")
    except (ValueError, TypeError):
        return Outcome(elapsed, "response was not readable JSON", {}, {})
    return Outcome(elapsed, "", {}, as_object(payload))


def _error_body(text: str) -> JsonObject:
    """The tool's `error` object, for the fields `inband_error` flattens away."""
    try:
        return as_object(as_object(cast(object, json.loads(text))).get("error"))
    except (ValueError, TypeError):
        return {}


def journey_for(persona: Persona) -> tuple[JourneyStep, ...]:
    """The step list as this persona experiences it.

    One step differs, and only in its verdict: a customer reads their own order
    back, and a guest is refused. Everything else — every payload, every id — is
    identical on purpose, so a difference in outcome is attributable to the
    server rather than to the suite having sent two different things.
    """
    if persona.authenticated:
        return UCP_JOURNEY

    return tuple(replace(step, refusal=GUEST_ORDER_REFUSAL) if step.tool == ORDER_GET else step for step in UCP_JOURNEY)


def _report_refusal(rep: Reporter, step: JourneyStep, outcome: Outcome, label: str) -> None:
    """Grade a step whose refusal is the pass condition."""
    expected = step.refusal
    if expected is None:  # pragma: no cover - only called when it is set
        return

    if not outcome.error:
        rep.tool_fail(
            step.tool,
            label,
            f"the request succeeded; a {expected.code} refusal is required here, and serving it "
            f"on a shared sales-channel key makes every order readable by id",
        )
        return

    code = str(outcome.error_body.get("code", ""))
    severity = str(outcome.error_body.get("severity", ""))
    if (code, severity) != (expected.code, expected.severity):
        rep.tool_fail(
            step.tool,
            label,
            f"refused as {code or '<no code>'}/{severity or '<no severity>'}, expected "
            f"{expected.code}/{expected.severity}: {outcome.error}",
        )
        return

    rep.tool_pass(step.tool, label, f"refused as required: {code}/{severity} [{outcome.elapsed:.1f}s]")


def run_ucp_journey(
    rep: Reporter,
    session: str,
    endpoint: Endpoint,
    allow_mutations: bool = False,
    query: str = "",
    persona: Persona = GUEST,
) -> Context:
    """Walk the buyer journey. Returns the context gathered along the way.

    Every step commits, so the whole journey is gated on `allow_mutations`. It is
    reported as skipped rather than silently omitted: a suite that quietly does
    less than it claims is worse than one that fails.
    """
    steps = journey_for(persona)
    if not allow_mutations:
        for step in steps:
            rep.tool_skip(
                step.tool,
                f"{step.tool} ({persona.name}: {step.detail})",
                "journey needs --allow-mutations: it places a real order",
            )
        return {}

    ctx: Context = {"query": query}
    if persona.authenticated:
        # Read by checkout.create, and the reason the order belongs to this
        # customer rather than to a guest registered on the spot.
        ctx["context_token"] = persona.context_token
    if code := os.environ.get(PROMO_CODE_ENV, ""):
        ctx["promo_code"] = code

    for step in steps:
        label = f"{step.tool} ({persona.name}: {step.detail})"
        if missing := step.missing(ctx):
            # Name the precondition, not the symptom. "needs cart_id" points at
            # the step that should have produced it; "not found" does not.
            rep.tool_skip(step.tool, label, f"precondition missing: {missing}")
            continue

        outcome = _call(session, endpoint, step, ctx)
        if step.refusal is not None:
            _report_refusal(rep, step, outcome, label)
            continue

        if outcome.error:
            rep.tool_fail(step.tool, label, f"{outcome.error} [{outcome.elapsed:.1f}s]")
            continue

        step.capture(outcome.payload, ctx)
        note = f"{outcome.elapsed:.1f}s"
        if outcome.elapsed > SLOW_STEP_S:
            note += " — slow"
        rep.tool_pass(step.tool, label, note)

    return ctx
