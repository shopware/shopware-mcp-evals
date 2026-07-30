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

# Tools this plugin owns. `shopware-store-api-` is excluded on purpose — see the
# module docstring.
TOOL_PREFIX = "shopware-ucp-"

# Reads. Safe to call for real.
READ_ONLY = frozenset(
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
DRY_RUNNABLE = frozenset(
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
UNSAFE = frozenset()

# Every UCP runtime tool rejects a request without this header. The SDK reads it
# with /profile="([^"]+)"/ and then fetches the URI, so it has to be a real
# document served by a host the instance allows — see assertSafeProfileUri in
# ucp-php-sdk. The shop's own published profile is the default because it always
# exists and needs no separate service.
#
# Note this is the shop's *service* profile, not an agent profile. It clears the
# `allowedProfileHosts` check but an instance whose `agentAllowlist` is set will
# still reject it, which is arguably correct: the eval is the agent, the shop is
# not. Override with UCP_PROFILE_URI when a real agent profile exists.
PROFILE_PATH = "/.well-known/ucp"
AGENT_NAME = os.environ.get("UCP_AGENT_NAME", "shopware-mcp-evals")


def is_ucp_tool(name: str) -> bool:
    return name.startswith(TOOL_PREFIX)


def agent_header(base_url: str, profile_uri: str | None = None) -> str:
    """The UCP-Agent header value for a shop at `base_url`."""
    uri = profile_uri or os.environ.get("UCP_PROFILE_URI") or f"{base_url.rstrip('/')}{PROFILE_PATH}"
    return f'{AGENT_NAME} profile="{uri}"'


def all_classified() -> frozenset[str]:
    return READ_ONLY | DRY_RUNNABLE | UNSAFE
