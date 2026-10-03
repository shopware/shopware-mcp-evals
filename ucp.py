#!/usr/bin/env python3
"""Everything specific to the agentic-commerce (UCP) plugin, in one place.

UCP is an optional plugin — `shopware/agentic-commerce`, checked out only when
the Store suite runs — and it may not be here forever. Keeping its specifics
scattered through mcp_client, toolclass and the runner would make removing it an
archaeology exercise, so it lives here instead. To drop UCP entirely: delete
this module, the `ucp.*` imports in `toolclass.py` and `mcp_client.py`, the UCP
fixtures in `eval/fixtures_store.yaml`, and the `UCP_TOOLS` entry in
`ownership.py`. Nothing else knows about it.

What is deliberately NOT here: `shopware-store-api-context`. Despite riding the
same endpoint, that tool is Shopware core (see the `shopware-store-api-` prefix
in ownership.py), so it survives the plugin's removal and is classified with the
rest of core.

The two things this module carries are the ones a caller cannot guess:

  * the execution classification, read off the live catalogue rather than
    inferred from names, and
  * the `UCP-Agent` header, without which every runtime tool rejects the call.

It also carries the lane lookups the Store eval's `{placeholder}`s resolve
through (`purchasable_product_id`, `seed_lane`), because they are UCP calls.
They take the tool call as a parameter rather than importing `mcp_client`,
which imports this module. `eval/runner.py` wires them in, so dropping UCP also
means deleting its `STORE_*_RESOLVERS` and the `_ucp_*` helpers beside them.
"""

import os
import uuid
from collections.abc import Callable

from eval.result_schema import JsonObject, as_list, as_object

# There is no prefix to match on any more. agentic-commerce 1.3.0 (UCP
# 2026-08-25) renamed every tool to the spec's canonical verb_noun names:
#
#   shopware-ucp-cart-create     ->  create_cart
#   shopware-ucp-catalog-search  ->  search_catalog
#   shopware-ucp-checkout-get    ->  get_checkout
#
# A UCP client discovers these by name, so they are the plugin's interop
# surface rather than Shopware's namespace, and nothing marks them as
# Shopware's. Membership is therefore the explicit set below — `all_classified()`
# — and a new tool that is not listed is simply not treated as UCP, which is the
# safe direction: it gets no Idempotency-Key and no execution class, so the
# functional runner refuses to call it rather than calling it blind.
#
# `shopware-store-api-context` is excluded on purpose — see the module docstring.

# Reads. Safe to call for real.
READ_ONLY: frozenset[str] = frozenset(
    {
        "get_cart",
        "get_checkout",
        "get_order",
        "lookup_catalog",
        "search_catalog",
    }
)

# Mutating, but the plugin declares `dryRun` on each, so they can be executed
# safely. These were guessed UNSAFE while the Store endpoint had no snapshot to
# read schemas from; the list below is taken from the live catalogue.
#
# `complete_checkout` is the one that can take money, and it is only callable at
# all because the server offers the safe path.
DRY_RUNNABLE: frozenset[str] = frozenset(
    {
        "apply_discount",
        "cancel_cart",
        "cancel_checkout",
        "complete_checkout",
        "create_cart",
        "create_checkout",
        "update_cart",
        "update_checkout",
    }
)

# Nothing currently. Kept so a new mutating tool without a dryRun has an obvious
# home rather than being forced into one of the two above.
UNSAFE: frozenset[str] = frozenset()

# Every UCP runtime tool rejects a request without this header. The SDK reads it
# with /profile="([^"]+)"/ and then fetches the URI, so it has to be a real
# document served by a host the instance allows — see UrlSafetyValidator in
# ucp-php-sdk. The shop's own published profile is the default because it always
# exists and needs no separate service.
#
# Note this is the shop's *service* profile, not an agent profile. Override with
# UCP_PROFILE_URI once a real agent profile exists; the shop's own is a stand-in.
#
# Two gates sit behind this, and only one of them is configurable:
#
#   agentAllowlist  falls back to the sales-channel domains when unset, so it
#                   passes by default. `ucp:config:set --agent-allowlist=<host>`
#                   fixes it when an instance has set one.
#   plain http      allowed only when the host is exactly localhost/127.0.0.1/::1
#                   AND SWAG_AGENTIC_COMMERCE_UCP_PROFILE_FETCHING_DEVELOPMENT_MODE
#                   is on. CI meets both (APP_URL is http://localhost:8000, and
#                   the workflow sets the flag).
#
# A local `<shop>.localhost` instance fails the host half upstream: the SDK's
# isLocalHost() is an exact match on the bare name, so a `.localhost` subdomain —
# loopback by RFC 6761 §6.3, and what every Shopware dev setup uses — is treated
# as remote and therefore required to be https. No setting reaches past it; it
# needs the one-line SDK change accepting the reserved TLD.
#
# Then set UCP_PROFILE_URI, because the *server* fetches this URI and it is not
# on the host's network. A shop published at `<shop>.localhost:8088` through a
# host proxy listens on :8000 inside its own container, so the published URI is
# connection-refused there and the fetch fails as an unlogged `internal` error.
# Point it at the port the container itself serves, keeping the host the
# agentAllowlist expects:
#
#   UCP_PROFILE_URI=http://<shop>.localhost:8000/.well-known/ucp
#
# CI needs none of this: APP_URL is http://localhost:8000, which is both the
# published URL and the one the server can reach.
PROFILE_PATH = "/.well-known/ucp"
AGENT_NAME = os.environ.get("UCP_AGENT_NAME", "shopware-mcp-evals")


def is_ucp_tool(name: str) -> bool:
    """Membership by name, because the catalogue no longer carries a prefix.

    Unknown names are not UCP. See the comment above READ_ONLY for why that is
    the safe default rather than a gap.
    """
    return name in all_classified()


def agent_header(base_url: str, profile_uri: str | None = None) -> str:
    """The UCP-Agent header value for a shop at `base_url`.

    The default derives the profile URI from `base_url`, which is THIS machine's
    address for the shop — and the SERVER is what fetches it, mid-request, over its
    own network. Those are the same host in CI, where the runner and the shop share
    `localhost:8000`, and different on any containerised or proxied lane: a shop
    published at `trunk.localhost:8088` reaches itself at `localhost:8000` and
    cannot resolve the published name at all.

    So the default is right where it is usually used and structurally wrong
    elsewhere, silently. `UCP_PROFILE_URI` is the override, and the failure now
    names itself — see the "could not be fetched" entry in eval/preflight.py's
    DIAGNOSES, which cost an afternoon to write.
    """
    uri = profile_uri or os.environ.get("UCP_PROFILE_URI") or f"{base_url.rstrip('/')}{PROFILE_PATH}"
    return f'{AGENT_NAME} profile="{uri}"'


def call_headers(tool: str) -> dict[str, str]:
    """Per-call headers for a UCP tool, empty for anything else.

    Mutating UCP operations are rejected outright when `idempotencyRequired` is
    on — which it is by default — so without this every dry run fails with
    "Idempotency key is required for mutating UCP requests" before the tool does
    any work. That reads like a tool-quality problem in the results and is not.

    A fresh key per call is deliberate. The key identifies one logical operation,
    and the server replays a completed response for a repeated one; reusing a key
    across fixtures would serve fixture A's answer to fixture B. Dry runs are
    also careful not to consume the key (see previewMutation in the plugin), so
    the two never collide.
    """
    if not is_ucp_tool(tool) or tool not in DRY_RUNNABLE | UNSAFE:
        return {}
    return {"Idempotency-Key": str(uuid.uuid4())}


def all_classified() -> frozenset[str]:
    return READ_ONLY | DRY_RUNNABLE | UNSAFE


# ---------------------------------------------------------------------------
# Lane lookups for the Store eval's placeholders
# ---------------------------------------------------------------------------
# Calls one UCP tool and returns the parsed result body, or {} when there is
# none to parse. Supplied by the caller: see the module docstring.
type ToolCall = Callable[[str, JsonObject], JsonObject]

# How many catalogue hits to try before giving up on finding one a cart takes.
PRODUCT_CANDIDATES = 10


def _data(body: JsonObject) -> JsonObject:
    """The answer inside a UCP result, or {} for a refusal.

    Every UCP tool wraps its answer as `{"success": true, "data": {...}}` and
    reports a refusal in-band as `success: false` with HTTP 200, so a refusal
    has to be read here rather than left to look like an empty answer.
    """
    if body.get("success") is False:
        return {}
    return as_object(body.get("data"))


def _line_items(product_id: str) -> list[object]:
    return [{"item": {"id": product_id}, "quantity": 1}]


def purchasable_product_id(call: ToolCall) -> str:
    """A product a cart on this sales channel accepts, or "".

    Searchable is not enough. The fixtures used to name a product the catalogue
    returns and a cart refuses ("not purchasable in this sales channel"), so
    the model built a correct create_cart and was graded `invalid_arguments`.
    Each candidate is therefore tried with a DRY-RUN create_cart, which writes
    nothing, and the first one it accepts is the answer.
    """
    found = _data(call("search_catalog", {"query": "", "limit": PRODUCT_CANDIDATES}))
    for row in as_list(found.get("products")):
        product_id = str(as_object(row).get("id") or "")
        if product_id and _data(
            call("create_cart", {"payload": {"line_items": _line_items(product_id)}, "dryRun": True})
        ):
            return product_id
    return ""


def seed_lane(call: ToolCall) -> dict[str, str]:
    """MUTATES: one real cart and one real checkout, as {cart_id, line_item_id, checkout_id}.

    Invented ids cost the Store suite five fixtures a night. update_cart and
    cancel_cart prompts named a cart that did not exist, so the model read it
    first, as an agent should, got "not found", and was graded for the read.
    And a dry-run complete_checkout on an invented checkout id answers
    "incomplete" rather than "not found", so those fixtures passed without the
    call proving anything.

    One cart, and its line item, so `{line_item_id}` names a line in the cart
    `{cart_id}` points at. Every UCP mutation the eval executes is a dry run
    (DRY_RUNNABLE), so fixtures that cancel this cart or checkout leave it in
    place for the ones running beside them.

    An id that could not be created comes back as "" and its fixtures are
    skipped by name.
    """
    product_id = purchasable_product_id(call)
    if not product_id:
        return {}
    lines = _line_items(product_id)
    cart = _data(call("create_cart", {"payload": {"line_items": lines}, "dryRun": False}))
    first_line = as_object(next(iter(as_list(cart.get("line_items"))), None))
    checkout = _data(call("create_checkout", {"payload": {"line_items": lines}, "dryRun": False}))
    return {
        "cart_id": str(cart.get("id") or ""),
        "line_item_id": str(first_line.get("id") or ""),
        "checkout_id": str(checkout.get("id") or ""),
    }
