#!/usr/bin/env python3
"""Everything specific to the agentic-commerce (UCP) plugin, in one place.

UCP is an optional plugin — `shopware/agentic-commerce`, checked out only when
the Store suite runs — and it may not be here forever. Keeping its specifics
scattered through mcp_client, toolclass and the runner would make removing it an
archaeology exercise, so it lives here instead. To drop UCP entirely: delete
this module, the `ucp.*` imports in `toolclass.py` and `mcp_client.py`, and the
`shopware-ucp-*` fixtures in `eval/fixtures_store.yaml`. Nothing else knows
about it.

What is deliberately NOT here: `shopware-store-api-context`. Despite riding the
same endpoint, that tool is Shopware core (see the `shopware-store-api-` prefix
in ownership.py), so it survives the plugin's removal and is classified with the
rest of core.

The two things this module carries are the ones a caller cannot guess:

  * the execution classification, read off the live catalogue rather than
    inferred from names, and
  * the `UCP-Agent` header, without which every runtime tool rejects the call.
"""

import os
import uuid

# Tools this plugin owns. `shopware-store-api-` is excluded on purpose — see the
# module docstring.
TOOL_PREFIX = "shopware-ucp-"

# Reads. Safe to call for real.
READ_ONLY: frozenset[str] = frozenset(
    {
        "shopware-ucp-cart-get",
        "shopware-ucp-catalog-lookup",
        "shopware-ucp-catalog-search",
        "shopware-ucp-checkout-get",
        "shopware-ucp-order-get",
    }
)

# Mutating, but the plugin declares `dryRun` on each, so they can be executed
# safely. These were guessed UNSAFE while the Store endpoint had no snapshot to
# read schemas from; the list below is taken from the live catalogue.
#
# `checkout-complete` is the one that can take money, and it is only callable at
# all because the server offers the safe path.
DRY_RUNNABLE: frozenset[str] = frozenset(
    {
        "shopware-ucp-cart-cancel",
        "shopware-ucp-cart-create",
        "shopware-ucp-cart-update",
        "shopware-ucp-checkout-cancel",
        "shopware-ucp-checkout-complete",
        "shopware-ucp-checkout-create",
        "shopware-ucp-checkout-update",
        "shopware-ucp-discount-apply",
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
    return name.startswith(TOOL_PREFIX)


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
